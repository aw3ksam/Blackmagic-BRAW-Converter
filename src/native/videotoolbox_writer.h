#pragma once

#import <Foundation/Foundation.h>
#import <AVFoundation/AVFoundation.h>
#import <VideoToolbox/VideoToolbox.h>
#import <CoreVideo/CoreVideo.h>
#import <CoreMedia/CoreMedia.h>
#import <Metal/Metal.h>

#include <iostream>
#include <string>
#include <vector>
#include <mutex>
#include <condition_variable>
#include <atomic>

class VideoToolboxWriter {
public:
    id<MTLDevice> device = nil;
    AVAssetWriter* writer = nil;
    AVAssetWriterInput* videoInput = nil;
    AVAssetWriterInputPixelBufferAdaptor* adaptor = nil;
    CVMetalTextureCacheRef textureCache = NULL;

    AVAssetReader* audioReader = nil;
    AVAssetReaderTrackOutput* audioReaderOutput = nil;
    AVAssetWriterInput* audioInput = nil;
    dispatch_queue_t audioQueue = nil;
    std::atomic<bool> audioFinished{false};

    uint32_t width = 0;
    uint32_t height = 0;
    float frameRate = 29.97f;
    uint64_t currentFrameIndex = 0;

    VideoToolboxWriter(id<MTLDevice> mtlDevice) : device(mtlDevice) {}

    ~VideoToolboxWriter() {
        cleanup();
    }

    void cleanup() {
        if (textureCache) {
            CVMetalTextureCacheFlush(textureCache, 0);
            CFRelease(textureCache);
            textureCache = NULL;
        }
    }

    bool initialize(
        const std::string& inputBrawPath,
        const std::string& outputPath,
        uint32_t clipWidth,
        uint32_t clipHeight,
        float clipFps,
        int bitrateMbps = 50,
        bool useMain10 = true,
        const std::string& codecType = "hevc"
    ) {
        width = clipWidth;
        height = clipHeight;
        frameRate = (clipFps > 0.0f) ? clipFps : 29.97f;

        NSError* error = nil;
        NSURL* outputUrl = [NSURL fileURLWithPath:[NSString stringWithUTF8String:outputPath.c_str()]];

        // Ensure parent directory exists
        [[NSFileManager defaultManager] createDirectoryAtURL:[outputUrl URLByDeletingLastPathComponent]
                                 withIntermediateDirectories:YES
                                                  attributes:nil
                                                       error:nil];

        // Remove destination if it already exists
        [[NSFileManager defaultManager] removeItemAtURL:outputUrl error:nil];

        writer = [[AVAssetWriter alloc] initWithURL:outputUrl fileType:AVFileTypeMPEG4 error:&error];
        if (!writer || error) {
            std::cerr << "Failed to initialize AVAssetWriter: " 
                      << (error ? [error.localizedDescription UTF8String] : "Unknown error") << std::endl;
            return false;
        }

        // 1. Configure Video Settings
        AVVideoCodecType avCodec = AVVideoCodecTypeHEVC;
        if (codecType == "h264" || codecType == "H264") {
            avCodec = AVVideoCodecTypeH264;
        }

        int64_t targetBps = (int64_t)bitrateMbps * 1000 * 1000;
        if (targetBps <= 0) {
            targetBps = 50 * 1000 * 1000;
        }

        NSMutableDictionary* compressionProps = [NSMutableDictionary dictionaryWithDictionary:@{
            AVVideoAverageBitRateKey: @(targetBps),
            AVVideoExpectedSourceFrameRateKey: @(frameRate),
            AVVideoMaxKeyFrameIntervalKey: @((int)(frameRate * 2)),
            (__bridge NSString*)kVTCompressionPropertyKey_RealTime: @NO,
            (__bridge NSString*)kVTCompressionPropertyKey_MaximizePowerEfficiency: @NO,
            (__bridge NSString*)kVTCompressionPropertyKey_AverageBitRate: @(targetBps),
            (__bridge NSString*)kVTCompressionPropertyKey_AllowFrameReordering: @YES,
        }];

        if (avCodec == AVVideoCodecTypeHEVC) {
            if (useMain10) {
                compressionProps[AVVideoProfileLevelKey] = (__bridge NSString*)kVTProfileLevel_HEVC_Main10_AutoLevel;
            } else {
                compressionProps[AVVideoProfileLevelKey] = (__bridge NSString*)kVTProfileLevel_HEVC_Main_AutoLevel;
            }
        }

        NSDictionary* videoSettings = @{
            AVVideoCodecKey: avCodec,
            AVVideoWidthKey: @(width),
            AVVideoHeightKey: @(height),
            AVVideoCompressionPropertiesKey: compressionProps,
            AVVideoColorPropertiesKey: @{
                AVVideoColorPrimariesKey: AVVideoColorPrimaries_ITU_R_709_2,
                AVVideoTransferFunctionKey: AVVideoTransferFunction_ITU_R_709_2,
                AVVideoYCbCrMatrixKey: AVVideoYCbCrMatrix_ITU_R_709_2,
            }
        };

        videoInput = [AVAssetWriterInput assetWriterInputWithMediaType:AVMediaTypeVideo outputSettings:videoSettings];
        videoInput.expectsMediaDataInRealTime = NO;

        NSDictionary* pixelBufferAttrs = @{
            (__bridge NSString*)kCVPixelBufferPixelFormatTypeKey: @(kCVPixelFormatType_32BGRA),
            (__bridge NSString*)kCVPixelBufferWidthKey: @(width),
            (__bridge NSString*)kCVPixelBufferHeightKey: @(height),
            (__bridge NSString*)kCVPixelBufferMetalCompatibilityKey: @YES,
            (__bridge NSString*)kCVPixelBufferIOSurfacePropertiesKey: @{},
        };

        adaptor = [AVAssetWriterInputPixelBufferAdaptor assetWriterInputPixelBufferAdaptorWithAssetWriterInput:videoInput
                                                                                   sourcePixelBufferAttributes:pixelBufferAttrs];

        if ([writer canAddInput:videoInput]) {
            [writer addInput:videoInput];
        } else {
            std::cerr << "Error: AVAssetWriter rejected video input." << std::endl;
            return false;
        }

        // 2. Configure Audio Pipeline from Input BRAW
        NSURL* inputUrl = [NSURL fileURLWithPath:[NSString stringWithUTF8String:inputBrawPath.c_str()]];
        AVURLAsset* inputAsset = [AVURLAsset URLAssetWithURL:inputUrl options:nil];
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
        NSArray<AVAssetTrack*>* audioTracks = [inputAsset tracksWithMediaType:AVMediaTypeAudio];
#pragma clang diagnostic pop

        if (audioTracks.count > 0) {
            AVAssetTrack* audioTrack = audioTracks.firstObject;
            audioReader = [AVAssetReader assetReaderWithAsset:inputAsset error:&error];
            if (audioReader) {
                NSDictionary* audioReadSettings = @{
                    AVFormatIDKey: @(kAudioFormatLinearPCM),
                    AVLinearPCMIsFloatKey: @NO,
                    AVLinearPCMBitDepthKey: @16,
                    AVLinearPCMIsNonInterleaved: @NO,
                };
                audioReaderOutput = [AVAssetReaderTrackOutput assetReaderTrackOutputWithTrack:audioTrack
                                                                              outputSettings:audioReadSettings];
                audioReaderOutput.alwaysCopiesSampleData = NO;

                if ([audioReader canAddOutput:audioReaderOutput]) {
                    [audioReader addOutput:audioReaderOutput];
                }

                // AAC Output Settings
                AudioChannelLayout channelLayout;
                bzero(&channelLayout, sizeof(channelLayout));
                channelLayout.mChannelLayoutTag = kAudioChannelLayoutTag_Stereo;

                NSDictionary* audioWriteSettings = @{
                    AVFormatIDKey: @(kAudioFormatMPEG4AAC),
                    AVNumberOfChannelsKey: @(2),
                    AVSampleRateKey: @(48000),
                    AVEncoderBitRateKey: @(320000),
                    AVChannelLayoutKey: [NSData dataWithBytes:&channelLayout length:sizeof(channelLayout)],
                };

                audioInput = [AVAssetWriterInput assetWriterInputWithMediaType:AVMediaTypeAudio
                                                               outputSettings:audioWriteSettings];
                audioInput.expectsMediaDataInRealTime = NO;

                if ([writer canAddInput:audioInput]) {
                    [writer addInput:audioInput];
                }
            }
        }

        // 3. Create Metal Texture Cache
        CVReturn cvRet = CVMetalTextureCacheCreate(kCFAllocatorDefault, NULL, device, NULL, &textureCache);
        if (cvRet != kCVReturnSuccess) {
            std::cerr << "Error: CVMetalTextureCacheCreate failed: " << cvRet << std::endl;
            return false;
        }

        return true;
    }

    bool start() {
        if (![writer startWriting]) {
            std::cerr << "Error: AVAssetWriter startWriting failed: " 
                      << [[writer.error localizedDescription] UTF8String] << std::endl;
            return false;
        }

        [writer startSessionAtSourceTime:kCMTimeZero];

        if (audioReader && audioReaderOutput && audioInput) {
            [audioReader startReading];
            audioQueue = dispatch_queue_create("com.braw.audio_transcode", DISPATCH_QUEUE_SERIAL);
            audioFinished = false;

            [audioInput requestMediaDataWhenReadyOnQueue:audioQueue usingBlock:^{
                while (audioInput.isReadyForMoreMediaData) {
                    CMSampleBufferRef sampleBuffer = [audioReaderOutput copyNextSampleBuffer];
                    if (sampleBuffer) {
                        [audioInput appendSampleBuffer:sampleBuffer];
                        CFRelease(sampleBuffer);
                    } else {
                        [audioInput markAsFinished];
                        audioFinished = true;
                        break;
                    }
                }
            }];
        } else {
            audioFinished = true;
        }

        return true;
    }

    CVPixelBufferRef createPixelBuffer(id<MTLTexture> __strong & outMetalTexture) {
        if (!adaptor.pixelBufferPool) {
            return NULL;
        }

        CVPixelBufferRef pixelBuffer = NULL;
        CVReturn status = CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, adaptor.pixelBufferPool, &pixelBuffer);
        if (status != kCVReturnSuccess || !pixelBuffer) {
            return NULL;
        }

        CVMetalTextureRef cvMtlTex = NULL;
        CVReturn ret = CVMetalTextureCacheCreateTextureFromImage(
            kCFAllocatorDefault,
            textureCache,
            pixelBuffer,
            NULL,
            MTLPixelFormatBGRA8Unorm,
            width,
            height,
            0,
            &cvMtlTex
        );

        if (ret == kCVReturnSuccess && cvMtlTex) {
            outMetalTexture = CVMetalTextureGetTexture(cvMtlTex);
            CFRelease(cvMtlTex);
        } else {
            CVPixelBufferRelease(pixelBuffer);
            return NULL;
        }

        return pixelBuffer;
    }

    bool appendFrame(CVPixelBufferRef pixelBuffer, uint64_t frameIndex) {
        if (!pixelBuffer || !videoInput || !adaptor) return false;

        int64_t timescale = (int64_t)std::round(frameRate * 1000.0);
        int64_t value = (int64_t)std::round((double)frameIndex * 1000.0);
        CMTime presentationTime = CMTimeMake(value, (int32_t)timescale);

        // Wait up to 10.0 seconds (5000 * 2ms) for hardware encoder backpressure to clear
        int retry = 0;
        const int maxRetries = 5000;
        while (!videoInput.isReadyForMoreMediaData && retry < maxRetries) {
            if (writer.status == AVAssetWriterStatusFailed || writer.status == AVAssetWriterStatusCancelled) {
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
            retry++;
        }

        if (!videoInput.isReadyForMoreMediaData) {
            std::string errStr = writer.error ? [[writer.error localizedDescription] UTF8String] : "Encoder input timed out waiting for ready state";
            std::cerr << "Error: Video input stalled at frame " << frameIndex 
                      << " (status " << (int)writer.status << "): " << errStr << std::endl;
            CVPixelBufferRelease(pixelBuffer);
            return false;
        }

        BOOL success = NO;
        @try {
            success = [adaptor appendPixelBuffer:pixelBuffer withPresentationTime:presentationTime];
        } @catch (NSException *e) {
            std::cerr << "Exception: Uncaught NSException in appendPixelBuffer at frame " << frameIndex 
                      << ": " << [[e reason] UTF8String] << std::endl;
            success = NO;
        }
        CVPixelBufferRelease(pixelBuffer);

        if (!success) {
            std::string errStr = writer.error ? [[writer.error localizedDescription] UTF8String] : "Unknown append failure";
            std::cerr << "Error: appendPixelBuffer failed for frame " << frameIndex << ": " 
                      << errStr << std::endl;
            return false;
        }

        return true;
    }

    bool finalize() {
        [videoInput markAsFinished];

        int audioWaitLimit = 0;
        while (!audioFinished && audioWaitLimit < 100) {
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
            audioWaitLimit++;
        }

        dispatch_semaphore_t sema = dispatch_semaphore_create(0);
        __block BOOL finishSuccess = YES;

        [writer finishWritingWithCompletionHandler:^{
            if (writer.status == AVAssetWriterStatusFailed) {
                std::cerr << "AVAssetWriter failed: " 
                          << [[writer.error localizedDescription] UTF8String] << std::endl;
                finishSuccess = NO;
            }
            dispatch_semaphore_signal(sema);
        }];

        dispatch_semaphore_wait(sema, DISPATCH_TIME_FOREVER);
        return (finishSuccess == YES);
    }
};
