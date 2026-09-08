#include <iostream>
#include <iomanip>
#include <string>
#include <vector>
#include <map>
#include <memory>
#include <atomic>
#include <mutex>
#include <condition_variable>
#include <thread>
#include <chrono>
#include <cmath>
#include <algorithm>
#include <unistd.h>
#include <fcntl.h>

#include <CoreFoundation/CoreFoundation.h>
#include <CoreServices/CoreServices.h>
#import <Metal/Metal.h>

#include "BlackmagicRawAPI.h"
#include "lut_3d_metal.h"
#include "videotoolbox_writer.h"

extern "C" IBlackmagicRawFactory* CreateBlackmagicRawFactoryInstanceFromPath(CFStringRef loadPath);
extern "C" IBlackmagicRawFactory* CreateBlackmagicRawFactoryInstanceFromExeRelativePath(CFStringRef relativePath);
extern "C" IBlackmagicRawFactory* CreateBlackmagicRawFactoryInstance(void);

static const size_t kBufferPoolSize = 4;
static const BlackmagicRawResourceFormat s_resourceFormat = blackmagicRawResourceFormatRGBAU8;

struct FrameBuffer {
    uint64_t frameIndex = 0;
    std::vector<uint8_t> data;
};

struct UserData {
    uint64_t frameIndex;
    size_t slotIndex;
};

// Streaming / CPU Fallback Callback Context
class BrawDecoderContext : public IBlackmagicRawCallback {
public:
    std::atomic<uint64_t> jobsInFlight{0};
    uint32_t maxJobsInFlight = 3;
    std::atomic<bool> failed{false};

    std::mutex queueMutex;
    std::condition_variable queueCv;
    std::condition_variable slotCv;
    std::map<uint64_t, std::shared_ptr<FrameBuffer>> readyFrames;

    IBlackmagicRawPipelineDevice* metalDevice = nullptr;
    id<MTLCommandQueue> metalCommandQueue = nil;
    bool useMetal = false;
    uint32_t scale = 1;

    // Pre-allocated reusable staging buffer pool
    id<MTLBuffer> stagingBufferPool[kBufferPoolSize];
    bool slotInUse[kBufferPoolSize];
    size_t bufferSizeBytes = 0;

    BrawDecoderContext() {
        for (size_t i = 0; i < kBufferPoolSize; ++i) {
            stagingBufferPool[i] = nil;
            slotInUse[i] = false;
        }
    }

    virtual ~BrawDecoderContext() {
        for (size_t i = 0; i < kBufferPoolSize; ++i) {
            stagingBufferPool[i] = nil;
        }
    }

    void initBufferPool(id<MTLDevice> device, size_t sizeBytes) {
        bufferSizeBytes = sizeBytes;
        for (size_t i = 0; i < kBufferPoolSize; ++i) {
            if (device != nil) {
                stagingBufferPool[i] = [device newBufferWithLength:sizeBytes options:MTLResourceStorageModeManaged];
            }
            slotInUse[i] = false;
        }
    }

    size_t acquireSlotBlocking() {
        std::unique_lock<std::mutex> lock(queueMutex);
        slotCv.wait(lock, [&]() {
            if (failed) return true;
            for (size_t i = 0; i < kBufferPoolSize; ++i) {
                if (!slotInUse[i]) return true;
            }
            return false;
        });

        if (failed) return 0;

        for (size_t i = 0; i < kBufferPoolSize; ++i) {
            if (!slotInUse[i]) {
                slotInUse[i] = true;
                return i;
            }
        }
        return 0;
    }

    void releaseSlot(size_t slotIndex) {
        std::lock_guard<std::mutex> lock(queueMutex);
        if (slotIndex < kBufferPoolSize) {
            slotInUse[slotIndex] = false;
        }
        slotCv.notify_one();
    }

    virtual void ReadComplete(IBlackmagicRawJob* readJob, HRESULT result, IBlackmagicRawFrame* frame) override {
        UserData* userData = nullptr;
        readJob->GetUserData((void**)&userData);

        if (result != S_OK || !frame) {
            std::cerr << "ReadComplete failed with HRESULT " << std::hex << result << std::dec << std::endl;
            failed = true;
            if (userData) {
                releaseSlot(userData->slotIndex);
                delete userData;
            }
            readJob->Release();
            --jobsInFlight;
            queueCv.notify_all();
            slotCv.notify_all();
            return;
        }

        frame->SetResourceFormat(s_resourceFormat);
        if (scale > 1) {
            frame->SetResolutionScale((BlackmagicRawResolutionScale)scale);
        }

        IBlackmagicRawJob* decodeJob = nullptr;
        result = frame->CreateJobDecodeAndProcessFrame(nullptr, nullptr, &decodeJob);
        if (result == S_OK && decodeJob) {
            decodeJob->SetUserData(userData);
            result = decodeJob->Submit();
        }

        if (result != S_OK) {
            std::cerr << "DecodeJob Submit failed with HRESULT " << std::hex << result << std::dec << std::endl;
            if (decodeJob) decodeJob->Release();
            if (userData) {
                releaseSlot(userData->slotIndex);
                delete userData;
            }
            failed = true;
            --jobsInFlight;
            queueCv.notify_all();
            slotCv.notify_all();
        }

        readJob->Release();
    }

    virtual void ProcessComplete(IBlackmagicRawJob* job, HRESULT result, IBlackmagicRawProcessedImage* processedImage) override {
        UserData* userData = nullptr;
        job->GetUserData((void**)&userData);
        uint64_t frameIndex = userData ? userData->frameIndex : 0;
        size_t slotIndex = userData ? userData->slotIndex : 0;
        if (userData) delete userData;
        job->Release();

        if (result != S_OK || !processedImage) {
            std::cerr << "ProcessComplete failed for frame " << frameIndex << " with HRESULT " << std::hex << result << std::dec << std::endl;
            releaseSlot(slotIndex);
            failed = true;
            --jobsInFlight;
            queueCv.notify_all();
            slotCv.notify_all();
            return;
        }

        uint32_t width = 0, height = 0;
        processedImage->GetWidth(&width);
        processedImage->GetHeight(&height);
        if (scale > 1) {
            width /= scale;
            height /= scale;
        }
        size_t exactFrameBytes = (size_t)width * height * 4;

        void* resource = nullptr;
        processedImage->GetResource(&resource);

        auto fb = std::make_shared<FrameBuffer>();
        fb->frameIndex = frameIndex;
        fb->data.resize(exactFrameBytes);

        @autoreleasepool {
            if (useMetal && metalCommandQueue != nil && resource != nullptr) {
                id<MTLBuffer> stagingBuffer = stagingBufferPool[slotIndex];
                if (stagingBuffer != nil) {
                    id<MTLCommandBuffer> cmdBuf = [metalCommandQueue commandBuffer];
                    id<MTLBlitCommandEncoder> blit = [cmdBuf blitCommandEncoder];

                    id<MTLBuffer> procMetal = (__bridge id<MTLBuffer>)resource;
                    [blit copyFromBuffer:procMetal sourceOffset:0 toBuffer:stagingBuffer destinationOffset:0 size:exactFrameBytes];
                    [blit synchronizeResource:stagingBuffer];
                    [blit endEncoding];

                    [cmdBuf commit];
                    [cmdBuf waitUntilCompleted];

                    std::memcpy(fb->data.data(), stagingBuffer.contents, exactFrameBytes);
                }
            } else {
                std::memcpy(fb->data.data(), resource, exactFrameBytes);
            }
        }

        releaseSlot(slotIndex);

        {
            std::lock_guard<std::mutex> lock(queueMutex);
            readyFrames[frameIndex] = fb;
        }

        --jobsInFlight;
        queueCv.notify_all();
    }

    virtual void DecodeComplete(IBlackmagicRawJob*, HRESULT) override {}
    virtual void TrimProgress(IBlackmagicRawJob*, float) override {}
    virtual void TrimComplete(IBlackmagicRawJob*, HRESULT) override {}
    virtual void SidecarMetadataParseWarning(IBlackmagicRawClip*, CFStringRef, uint32_t, CFStringRef) override {}
    virtual void SidecarMetadataParseError(IBlackmagicRawClip*, CFStringRef, uint32_t, CFStringRef) override {}
    virtual void PreparePipelineComplete(void*, HRESULT) override {}

    virtual HRESULT STDMETHODCALLTYPE QueryInterface(REFIID, LPVOID*) override { return E_NOTIMPL; }
    virtual ULONG STDMETHODCALLTYPE AddRef(void) override { return 1; }
    virtual ULONG STDMETHODCALLTYPE Release(void) override { return 1; }
};

// High-Performance In-Process VideoToolbox Transcode Context
class BrawTranscodeContext : public IBlackmagicRawCallback {
public:
    std::atomic<uint64_t> jobsInFlight{0};
    uint32_t maxJobsInFlight = 6;
    std::atomic<bool> failed{false};

    std::mutex queueMutex;
    std::condition_variable queueCv;

    IBlackmagicRawPipelineDevice* metalDevice = nullptr;
    id<MTLCommandQueue> metalCommandQueue = nil;
    uint32_t scale = 1;

    MetalLutProcessor* lutProcessor = nullptr;
    VideoToolboxWriter* writer = nullptr;

    // Map of ready decoded frames waiting for sequential encode
    std::map<uint64_t, CVPixelBufferRef> readyPixelBuffers;

    BrawTranscodeContext() {}

    virtual void ReadComplete(IBlackmagicRawJob* readJob, HRESULT result, IBlackmagicRawFrame* frame) override {
        uint64_t frameIndex = 0;
        readJob->GetUserData((void**)&frameIndex);

        if (result != S_OK || !frame) {
            std::cerr << "ReadComplete failed for frame " << frameIndex << std::endl;
            failed = true;
            readJob->Release();
            --jobsInFlight;
            queueCv.notify_all();
            return;
        }

        frame->SetResourceFormat(s_resourceFormat);
        if (scale > 1) {
            frame->SetResolutionScale((BlackmagicRawResolutionScale)scale);
        }

        IBlackmagicRawJob* decodeJob = nullptr;
        result = frame->CreateJobDecodeAndProcessFrame(nullptr, nullptr, &decodeJob);
        if (result == S_OK && decodeJob) {
            decodeJob->SetUserData((void*)frameIndex);
            result = decodeJob->Submit();
        }

        if (result != S_OK) {
            std::cerr << "DecodeJob Submit failed for frame " << frameIndex << std::endl;
            if (decodeJob) decodeJob->Release();
            failed = true;
            --jobsInFlight;
            queueCv.notify_all();
        }

        readJob->Release();
    }

    virtual void ProcessComplete(IBlackmagicRawJob* job, HRESULT result, IBlackmagicRawProcessedImage* processedImage) override {
        uint64_t frameIndex = 0;
        job->GetUserData((void**)&frameIndex);
        job->Release();

        if (result != S_OK || !processedImage) {
            std::cerr << "ProcessComplete failed for frame " << frameIndex << std::endl;
            failed = true;
            --jobsInFlight;
            queueCv.notify_all();
            return;
        }

        void* resource = nullptr;
        processedImage->GetResource(&resource);
        id<MTLBuffer> procMetal = (__bridge id<MTLBuffer>)resource;

        if (procMetal && writer && lutProcessor && metalCommandQueue) {
            @autoreleasepool {
                id<MTLTexture> outTexture = nil;
                CVPixelBufferRef pixelBuffer = writer->createPixelBuffer(outTexture);

                if (pixelBuffer && outTexture) {
                    id<MTLCommandBuffer> cmdBuf = [metalCommandQueue commandBuffer];
                    lutProcessor->encodeColorGradingFromBuffer(cmdBuf, procMetal, outTexture);
                    
                    [cmdBuf addCompletedHandler:^(id<MTLCommandBuffer> buffer) {
                        {
                            std::lock_guard<std::mutex> lock(queueMutex);
                            readyPixelBuffers[frameIndex] = pixelBuffer;
                        }
                        queueCv.notify_all();
                    }];
                    [cmdBuf commit];
                } else {
                    std::cerr << "Error: Failed to allocate CVPixelBuffer for frame " << frameIndex << std::endl;
                    failed = true;
                }
            }
        }

        --jobsInFlight;
        queueCv.notify_all();
    }

    virtual void DecodeComplete(IBlackmagicRawJob*, HRESULT) override {}
    virtual void TrimProgress(IBlackmagicRawJob*, float) override {}
    virtual void TrimComplete(IBlackmagicRawJob*, HRESULT) override {}
    virtual void SidecarMetadataParseWarning(IBlackmagicRawClip*, CFStringRef, uint32_t, CFStringRef) override {}
    virtual void SidecarMetadataParseError(IBlackmagicRawClip*, CFStringRef, uint32_t, CFStringRef) override {}
    virtual void PreparePipelineComplete(void*, HRESULT) override {}

    virtual HRESULT STDMETHODCALLTYPE QueryInterface(REFIID, LPVOID*) override { return E_NOTIMPL; }
    virtual ULONG STDMETHODCALLTYPE AddRef(void) override { return 1; }
    virtual ULONG STDMETHODCALLTYPE Release(void) override { return 1; }
};

static std::string CFStringToStdString(CFStringRef cfStr) {
    if (!cfStr) return "";
    CFIndex length = CFStringGetLength(cfStr);
    CFIndex maxSize = CFStringGetMaximumSizeForEncoding(length, kCFStringEncodingUTF8) + 1;
    std::vector<char> buffer(maxSize);
    if (CFStringGetCString(cfStr, buffer.data(), maxSize, kCFStringEncodingUTF8)) {
        return std::string(buffer.data());
    }
    return "";
}

static IBlackmagicRawFactory* InitFactory() {
    IBlackmagicRawFactory* factory = nullptr;
    const char* sdkLocations[] = {
        "/Users/studio/Documents/Sandbox/davinci-braw/Documents/Blackmagic RAW SDK/Mac/Libraries",
        "Documents/Blackmagic RAW SDK/Mac/Libraries",
        "../Documents/Blackmagic RAW SDK/Mac/Libraries",
        "/Library/Application Support/Blackmagic Design/Blackmagic RAW",
        "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries"
    };

    for (const char* loc : sdkLocations) {
        CFStringRef cfLoc = CFStringCreateWithCString(NULL, loc, kCFStringEncodingUTF8);
        factory = CreateBlackmagicRawFactoryInstanceFromPath(cfLoc);
        CFRelease(cfLoc);
        if (factory) return factory;
    }

    factory = CreateBlackmagicRawFactoryInstance();
    return factory;
}

static int PrintClipInfo(const std::string& filePath) {
    IBlackmagicRawFactory* factory = InitFactory();
    if (!factory) {
        std::cerr << "Error: Failed to initialize Blackmagic RAW SDK Factory." << std::endl;
        return 1;
    }

    IBlackmagicRaw* codec = nullptr;
    if (factory->CreateCodec(&codec) != S_OK || !codec) {
        std::cerr << "Error: Failed to create codec." << std::endl;
        factory->Release();
        return 1;
    }

    CFStringRef cfPath = CFStringCreateWithCString(NULL, filePath.c_str(), kCFStringEncodingUTF8);
    IBlackmagicRawClip* clip = nullptr;
    if (codec->OpenClip(cfPath, &clip) != S_OK || !clip) {
        std::cerr << "Error: Failed to open clip: " << filePath << std::endl;
        CFRelease(cfPath);
        codec->Release();
        factory->Release();
        return 1;
    }

    uint32_t width = 0, height = 0;
    clip->GetWidth(&width);
    clip->GetHeight(&height);

    uint64_t frameCount = 0;
    clip->GetFrameCount(&frameCount);

    float frameRate = 0.0f;
    clip->GetFrameRate(&frameRate);

    CFStringRef cfTimecode = nullptr;
    std::string timecodeStr = "00:00:00:00";
    if (clip->GetTimecodeForFrame(0, &cfTimecode) == S_OK && cfTimecode) {
        timecodeStr = CFStringToStdString(cfTimecode);
        CFRelease(cfTimecode);
    }

    // Audio
    IBlackmagicRawClipAudio* audio = nullptr;
    bool hasAudio = false;
    uint32_t audioSampleRate = 48000;
    uint32_t audioChannels = 2;
    uint32_t audioBitDepth = 24;
    uint64_t audioSamples = 0;

    if (clip->QueryInterface(IID_IBlackmagicRawClipAudio, (void**)&audio) == S_OK && audio) {
        if (audio->GetAudioSampleCount(&audioSamples) == S_OK && audioSamples > 0) {
            hasAudio = true;
            audio->GetAudioSampleRate(&audioSampleRate);
            audio->GetAudioChannelCount(&audioChannels);
            audio->GetAudioBitDepth(&audioBitDepth);
        }
        audio->Release();
    }

    CFStringRef cfCameraType = nullptr;
    std::string cameraType = "Blackmagic Camera";
    if (clip->GetCameraType(&cfCameraType) == S_OK && cfCameraType) {
        cameraType = CFStringToStdString(cfCameraType);
        CFRelease(cfCameraType);
    }

    float durationSec = (frameRate > 0.0f) ? ((float)frameCount / frameRate) : 0.0f;

    std::cout << "{\n";
    std::cout << "  \"path\": \"" << filePath << "\",\n";
    std::cout << "  \"width\": " << width << ",\n";
    std::cout << "  \"height\": " << height << ",\n";
    std::cout << "  \"frame_count\": " << frameCount << ",\n";
    std::cout << "  \"frame_rate\": " << std::fixed << std::setprecision(3) << frameRate << ",\n";
    std::cout << "  \"duration_seconds\": " << std::fixed << std::setprecision(3) << durationSec << ",\n";
    std::cout << "  \"timecode\": \"" << timecodeStr << "\",\n";
    std::cout << "  \"has_audio\": " << (hasAudio ? "true" : "false") << ",\n";
    std::cout << "  \"audio_channels\": " << audioChannels << ",\n";
    std::cout << "  \"audio_sample_rate\": " << audioSampleRate << ",\n";
    std::cout << "  \"audio_bit_depth\": " << audioBitDepth << ",\n";
    std::cout << "  \"camera_type\": \"" << cameraType << "\"\n";
    std::cout << "}\n";

    clip->Release();
    codec->Release();
    factory->Release();
    CFRelease(cfPath);
    return 0;
}

// In-Process High-Speed Transcoding Engine
static int TranscodeClipNative(
    const std::string& inputPath,
    const std::string& outputPath,
    const std::string& lutPath,
    const std::string& codecType,
    int bitrateMbps,
    bool useMain10,
    uint64_t startFrame,
    uint64_t frameLimit,
    uint32_t scale
) {
    IBlackmagicRawFactory* factory = InitFactory();
    if (!factory) {
        std::cerr << "Error: Failed to initialize Blackmagic RAW SDK Factory." << std::endl;
        return 1;
    }

    IBlackmagicRaw* codec = nullptr;
    if (factory->CreateCodec(&codec) != S_OK || !codec) {
        std::cerr << "Error: Failed to create codec." << std::endl;
        factory->Release();
        return 1;
    }

    IBlackmagicRawConfiguration* config = nullptr;
    IBlackmagicRawPipelineDevice* metalDevice = nullptr;
    id<MTLDevice> nativeMtlDevice = nil;
    id<MTLCommandQueue> nativeMtlQueue = nil;

    IBlackmagicRawPipelineDeviceIterator* deviceIt = nullptr;
    if (factory->CreatePipelineDeviceIterator(blackmagicRawPipelineMetal, blackmagicRawInteropOpenGL, &deviceIt) == S_OK && deviceIt) {
        if (deviceIt->CreateDevice(&metalDevice) == S_OK && metalDevice) {
            if (codec->QueryInterface(IID_IBlackmagicRawConfiguration, (void**)&config) == S_OK && config) {
                if (config->SetFromDevice(metalDevice) == S_OK) {
                    BlackmagicRawPipeline pipeline;
                    void* ctx = nullptr;
                    void* cmdQueue = nullptr;
                    if (metalDevice->GetPipeline(&pipeline, &ctx, &cmdQueue) == S_OK && cmdQueue) {
                        nativeMtlQueue = (__bridge id<MTLCommandQueue>)cmdQueue;
                        nativeMtlDevice = nativeMtlQueue.device;
                    }
                }
            }
        }
        deviceIt->Release();
    }

    if (!nativeMtlDevice || !nativeMtlQueue) {
        std::cerr << "Error: Metal device initialization failed." << std::endl;
        if (config) config->Release();
        if (metalDevice) metalDevice->Release();
        codec->Release();
        factory->Release();
        return 1;
    }

    CFStringRef cfPath = CFStringCreateWithCString(NULL, inputPath.c_str(), kCFStringEncodingUTF8);
    IBlackmagicRawClip* clip = nullptr;
    if (codec->OpenClip(cfPath, &clip) != S_OK || !clip) {
        std::cerr << "Error: Failed to open clip: " << inputPath << std::endl;
        CFRelease(cfPath);
        if (config) config->Release();
        if (metalDevice) metalDevice->Release();
        codec->Release();
        factory->Release();
        return 1;
    }

    uint64_t totalFrames = 0;
    clip->GetFrameCount(&totalFrames);
    uint32_t width = 0, height = 0;
    clip->GetWidth(&width);
    clip->GetHeight(&height);
    float frameRate = 29.97f;
    clip->GetFrameRate(&frameRate);

    if (scale > 1) {
        width /= scale;
        height /= scale;
    }

    uint64_t endFrame = (frameLimit > 0 && (startFrame + frameLimit) < totalFrames) ? (startFrame + frameLimit) : totalFrames;
    uint64_t framesToProcess = (endFrame > startFrame) ? (endFrame - startFrame) : 0;

    std::cerr << "Starting In-Process Metal + VideoToolbox Transcode: " << width << "x" << height 
              << " @ " << frameRate << " fps (" << framesToProcess << " frames)" << std::endl;

    // Initialize 3D LUT Processor
    MetalLutProcessor lutProcessor(nativeMtlDevice);
    if (!lutPath.empty()) {
        lutProcessor.loadCubeLut(lutPath);
    }

    // Initialize VideoToolbox Writer
    VideoToolboxWriter writer(nativeMtlDevice);
    if (!writer.initialize(inputPath, outputPath, width, height, frameRate, bitrateMbps, useMain10, codecType)) {
        std::cerr << "Error: Failed to initialize VideoToolbox writer." << std::endl;
        clip->Release();
        if (config) config->Release();
        if (metalDevice) metalDevice->Release();
        codec->Release();
        factory->Release();
        CFRelease(cfPath);
        return 1;
    }

    if (!writer.start()) {
        std::cerr << "Error: Failed to start VideoToolbox writer." << std::endl;
        clip->Release();
        if (config) config->Release();
        if (metalDevice) metalDevice->Release();
        codec->Release();
        factory->Release();
        CFRelease(cfPath);
        return 1;
    }

    BrawTranscodeContext context;
    context.metalDevice = metalDevice;
    context.metalCommandQueue = nativeMtlQueue;
    context.scale = scale;
    context.lutProcessor = &lutProcessor;
    context.writer = &writer;

    if (codec->SetCallback(&context) != S_OK) {
        std::cerr << "Error: Failed to set BRAW codec callback." << std::endl;
        clip->Release();
        if (config) config->Release();
        if (metalDevice) metalDevice->Release();
        codec->Release();
        factory->Release();
        CFRelease(cfPath);
        return 1;
    }

    auto startTime = std::chrono::steady_clock::now();
    uint64_t nextFrameToSubmit = startFrame;
    uint64_t nextFrameToEncode = startFrame;

    while ((nextFrameToEncode < endFrame) && !context.failed) {
        // 1. Submit read jobs
        while ((nextFrameToSubmit < endFrame) && (context.jobsInFlight < context.maxJobsInFlight) && !context.failed) {
            IBlackmagicRawJob* readJob = nullptr;
            if (clip->CreateJobReadFrame(nextFrameToSubmit, &readJob) == S_OK && readJob) {
                readJob->SetUserData((void*)nextFrameToSubmit);
                ++context.jobsInFlight;
                if (readJob->Submit() != S_OK) {
                    readJob->Release();
                    --context.jobsInFlight;
                    context.failed = true;
                    break;
                }
                nextFrameToSubmit++;
            } else {
                context.failed = true;
                break;
            }
        }

        // 2. Retrieve sequential pixel buffer
        CVPixelBufferRef pixelBufferToEncode = NULL;
        {
            std::unique_lock<std::mutex> lock(context.queueMutex);
            context.queueCv.wait(lock, [&]() {
                return (context.readyPixelBuffers.find(nextFrameToEncode) != context.readyPixelBuffers.end()) || context.failed;
            });

            if (context.failed) break;

            auto it = context.readyPixelBuffers.find(nextFrameToEncode);
            if (it != context.readyPixelBuffers.end()) {
                pixelBufferToEncode = it->second;
                context.readyPixelBuffers.erase(it);
            }
        }

        // 3. Append frame to VideoToolbox HEVC encoder
        if (pixelBufferToEncode) {
            uint64_t relativeFrame = nextFrameToEncode - startFrame;
            if (!writer.appendFrame(pixelBufferToEncode, relativeFrame)) {
                std::cerr << "Failed to append frame " << nextFrameToEncode << std::endl;
                context.failed = true;
                break;
            }

            nextFrameToEncode++;
            uint64_t processedCount = nextFrameToEncode - startFrame;

            if (processedCount % 5 == 0 || nextFrameToEncode == endFrame) {
                auto now = std::chrono::steady_clock::now();
                double elapsedSec = std::chrono::duration<double>(now - startTime).count();
                double fps = (elapsedSec > 0.001) ? (double)processedCount / elapsedSec : 0.0;
                double pct = (framesToProcess > 0) ? (100.0 * (double)processedCount / (double)framesToProcess) : 100.0;

                std::cerr << "PROGRESS:{\"frame\":" << processedCount << ",\"total\":" << framesToProcess 
                          << ",\"percent\":" << std::fixed << std::setprecision(1) << pct 
                          << ",\"fps\":" << std::fixed << std::setprecision(1) << fps << "}" << std::endl;
            }
        }
    }

    codec->FlushJobs();

    bool finishOk = writer.finalize();

    if (config) config->Release();
    if (metalDevice) metalDevice->Release();
    clip->Release();
    codec->Release();
    factory->Release();
    CFRelease(cfPath);

    if (context.failed || !finishOk) {
        std::cerr << "Transcode finished with failure." << std::endl;
        return 1;
    }

    auto totalTime = std::chrono::steady_clock::now();
    double totalSec = std::chrono::duration<double>(totalTime - startTime).count();
    double avgFps = (totalSec > 0.001) ? (double)framesToProcess / totalSec : 0.0;
    std::cerr << "Transcode completed successfully in " << std::fixed << std::setprecision(2) 
              << totalSec << "s (" << std::fixed << std::setprecision(1) << avgFps << " fps)" << std::endl;

    return 0;
}

static int StreamClipFrames(const std::string& filePath, uint64_t startFrame, uint64_t frameLimit, bool useGpu, uint32_t scale) {
    #if defined(_WIN32)
        _setmode(_fileno(stdout), _O_BINARY);
    #endif

    IBlackmagicRawFactory* factory = InitFactory();
    if (!factory) {
        std::cerr << "Error: Failed to initialize Blackmagic RAW SDK Factory." << std::endl;
        return 1;
    }

    IBlackmagicRaw* codec = nullptr;
    if (factory->CreateCodec(&codec) != S_OK || !codec) {
        std::cerr << "Error: Failed to create codec." << std::endl;
        factory->Release();
        return 1;
    }

    BrawDecoderContext context;
    context.scale = scale;

    IBlackmagicRawConfiguration* config = nullptr;
    IBlackmagicRawPipelineDevice* metalDevice = nullptr;
    id<MTLDevice> nativeMtlDevice = nil;

    if (useGpu) {
        IBlackmagicRawPipelineDeviceIterator* deviceIt = nullptr;
        if (factory->CreatePipelineDeviceIterator(blackmagicRawPipelineMetal, blackmagicRawInteropOpenGL, &deviceIt) == S_OK && deviceIt) {
            if (deviceIt->CreateDevice(&metalDevice) == S_OK && metalDevice) {
                if (codec->QueryInterface(IID_IBlackmagicRawConfiguration, (void**)&config) == S_OK && config) {
                    if (config->SetFromDevice(metalDevice) == S_OK) {
                        context.metalDevice = metalDevice;
                        context.useMetal = true;

                        BlackmagicRawPipeline pipeline;
                        void* ctx = nullptr;
                        void* cmdQueue = nullptr;
                        if (metalDevice->GetPipeline(&pipeline, &ctx, &cmdQueue) == S_OK && cmdQueue) {
                            context.metalCommandQueue = (__bridge id<MTLCommandQueue>)cmdQueue;
                            nativeMtlDevice = context.metalCommandQueue.device;
                        }
                        std::cerr << "BRAW Decoder: Metal GPU Acceleration Enabled (Synchronized Managed Staging Ring)." << std::endl;
                    }
                }
            }
            deviceIt->Release();
        }
    }

    if (!context.useMetal) {
        std::cerr << "BRAW Decoder: Using Multi-Threaded CPU Pipeline." << std::endl;
    }

    CFStringRef cfPath = CFStringCreateWithCString(NULL, filePath.c_str(), kCFStringEncodingUTF8);
    IBlackmagicRawClip* clip = nullptr;
    if (codec->OpenClip(cfPath, &clip) != S_OK || !clip) {
        std::cerr << "Error: Failed to open clip: " << filePath << std::endl;
        CFRelease(cfPath);
        if (config) config->Release();
        if (metalDevice) metalDevice->Release();
        codec->Release();
        factory->Release();
        return 1;
    }

    uint64_t totalFrames = 0;
    clip->GetFrameCount(&totalFrames);
    uint32_t width = 0, height = 0;
    clip->GetWidth(&width);
    clip->GetHeight(&height);
    float frameRate = 0.0f;
    clip->GetFrameRate(&frameRate);

    if (scale > 1) {
        width /= scale;
        height /= scale;
    }

    size_t rgbaFrameBytes = (size_t)width * height * 4;
    context.initBufferPool(nativeMtlDevice, rgbaFrameBytes);

    uint64_t endFrame = (frameLimit > 0 && (startFrame + frameLimit) < totalFrames) ? (startFrame + frameLimit) : totalFrames;
    uint64_t framesToProcess = (endFrame > startFrame) ? (endFrame - startFrame) : 0;

    std::cerr << "BRAW Stream: " << width << "x" << height << " (RGBA) @ " << frameRate << " fps, Frames: " 
              << startFrame << ".." << endFrame << " (" << framesToProcess << " total)" << std::endl;

    if (codec->SetCallback(&context) != S_OK) {
        std::cerr << "Error: Failed to set BRAW codec callback." << std::endl;
        clip->Release();
        if (config) config->Release();
        if (metalDevice) metalDevice->Release();
        codec->Release();
        factory->Release();
        CFRelease(cfPath);
        return 1;
    }

    auto startTime = std::chrono::steady_clock::now();
    uint64_t nextFrameToSubmit = startFrame;
    uint64_t nextFrameToWrite = startFrame;

    while ((nextFrameToWrite < endFrame) && !context.failed) {
        while ((nextFrameToSubmit < endFrame) && (context.jobsInFlight < context.maxJobsInFlight) && !context.failed) {
            size_t slot = context.acquireSlotBlocking();
            if (context.failed) break;

            IBlackmagicRawJob* readJob = nullptr;
            if (clip->CreateJobReadFrame(nextFrameToSubmit, &readJob) == S_OK && readJob) {
                UserData* ud = new UserData();
                ud->frameIndex = nextFrameToSubmit;
                ud->slotIndex = slot;
                readJob->SetUserData(ud);

                ++context.jobsInFlight;
                if (readJob->Submit() != S_OK) {
                    delete ud;
                    context.releaseSlot(slot);
                    readJob->Release();
                    --context.jobsInFlight;
                    context.failed = true;
                    break;
                }
                nextFrameToSubmit++;
            } else {
                context.releaseSlot(slot);
                context.failed = true;
                break;
            }
        }

        std::shared_ptr<FrameBuffer> frameToWrite = nullptr;
        {
            std::unique_lock<std::mutex> lock(context.queueMutex);
            context.queueCv.wait(lock, [&]() {
                return (context.readyFrames.find(nextFrameToWrite) != context.readyFrames.end()) || context.failed;
            });

            if (context.failed) break;

            auto it = context.readyFrames.find(nextFrameToWrite);
            if (it != context.readyFrames.end()) {
                frameToWrite = it->second;
                context.readyFrames.erase(it);
            }
        }

        if (frameToWrite && !frameToWrite->data.empty()) {
            const uint8_t* ptr = frameToWrite->data.data();
            size_t remaining = frameToWrite->data.size();
            while (remaining > 0) {
                size_t written = fwrite(ptr, 1, remaining, stdout);
                if (written == 0) {
                    std::cerr << "Pipe broken or stdout write failed." << std::endl;
                    context.failed = true;
                    break;
                }
                ptr += written;
                remaining -= written;
            }
            fflush(stdout);

            nextFrameToWrite++;
            uint64_t processedCount = nextFrameToWrite - startFrame;
            if (processedCount % 5 == 0 || nextFrameToWrite == endFrame) {
                auto now = std::chrono::steady_clock::now();
                double elapsedSec = std::chrono::duration<double>(now - startTime).count();
                double fps = (elapsedSec > 0.001) ? (double)processedCount / elapsedSec : 0.0;
                double pct = (framesToProcess > 0) ? (100.0 * (double)processedCount / (double)framesToProcess) : 100.0;

                std::cerr << "PROGRESS:{\"frame\":" << processedCount << ",\"total\":" << framesToProcess 
                          << ",\"percent\":" << std::fixed << std::setprecision(1) << pct 
                          << ",\"fps\":" << std::fixed << std::setprecision(1) << fps << "}" << std::endl;
            }
        }
    }

    codec->FlushJobs();

    if (config) config->Release();
    if (metalDevice) metalDevice->Release();
    clip->Release();
    codec->Release();
    factory->Release();
    CFRelease(cfPath);

    return context.failed ? 1 : 0;
}

int main(int argc, char* argv[]) {
    if (argc < 2) {
        std::cerr << "Usage:" << std::endl;
        std::cerr << "  braw_decode --info <input.braw>" << std::endl;
        std::cerr << "  braw_decode --transcode <input.braw> -o <output.mp4> [--lut <lut.cube>] [--codec hevc|h264] [--bitrate N] [--main10]" << std::endl;
        std::cerr << "  braw_decode --stream <input.braw> [--start N] [--count N] [--scale 1|2|4|8] [--no-gpu]" << std::endl;
        return 1;
    }

    std::string mode = argv[1];
    if (mode == "--info" && argc >= 3) {
        return PrintClipInfo(argv[2]);
    } else if (mode == "--transcode" && argc >= 3) {
        std::string inputPath = argv[2];
        std::string outputPath = "";
        std::string lutPath = "";
        std::string codecType = "hevc";
        int bitrateMbps = 50;
        bool useMain10 = true;
        uint64_t startFrame = 0;
        uint64_t frameCount = 0;
        uint32_t scale = 1;

        for (int i = 3; i < argc; ++i) {
            std::string arg = argv[i];
            if ((arg == "-o" || arg == "--output") && (i + 1 < argc)) {
                outputPath = argv[++i];
            } else if (arg == "--lut" && (i + 1 < argc)) {
                lutPath = argv[++i];
            } else if (arg == "--codec" && (i + 1 < argc)) {
                codecType = argv[++i];
            } else if (arg == "--bitrate" && (i + 1 < argc)) {
                bitrateMbps = std::stoi(argv[++i]);
            } else if (arg == "--main10") {
                useMain10 = true;
            } else if (arg == "--main") {
                useMain10 = false;
            } else if (arg == "--start" && (i + 1 < argc)) {
                startFrame = std::stoull(argv[++i]);
            } else if (arg == "--count" && (i + 1 < argc)) {
                frameCount = std::stoull(argv[++i]);
            } else if (arg == "--scale" && (i + 1 < argc)) {
                scale = (uint32_t)std::stoul(argv[++i]);
            }
        }

        if (outputPath.empty()) {
            std::cerr << "Error: --output (-o) parameter required for transcode." << std::endl;
            return 1;
        }

        return TranscodeClipNative(inputPath, outputPath, lutPath, codecType, bitrateMbps, useMain10, startFrame, frameCount, scale);
    } else if (mode == "--stream" && argc >= 3) {
        std::string filePath = argv[2];
        bool useGpu = true;
        uint64_t startFrame = 0;
        uint64_t frameCount = 0;
        uint32_t scale = 1;

        for (int i = 3; i < argc; ++i) {
            std::string arg = argv[i];
            if (arg == "--no-gpu") {
                useGpu = false;
            } else if (arg == "--format" && (i + 1 < argc)) {
                ++i;
            } else if (arg == "--start" && (i + 1 < argc)) {
                startFrame = std::stoull(argv[++i]);
            } else if (arg == "--count" && (i + 1 < argc)) {
                frameCount = std::stoull(argv[++i]);
            } else if (arg == "--scale" && (i + 1 < argc)) {
                scale = (uint32_t)std::stoul(argv[++i]);
            }
        }

        return StreamClipFrames(filePath, startFrame, frameCount, useGpu, scale);
    } else {
        std::cerr << "Invalid arguments. Use --info, --transcode, or --stream." << std::endl;
        return 1;
    }
}
