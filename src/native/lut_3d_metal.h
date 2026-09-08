#pragma once

#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <CoreVideo/CoreVideo.h>
#import <CoreMedia/CoreMedia.h>
#import <AVFoundation/AVFoundation.h>
#import <VideoToolbox/VideoToolbox.h>

#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include <memory>
#include <chrono>

// MSL Metal Shader Source for Zero-Copy GPU 3D LUT Color Grading
static const char* kMetalLutShaderSource = R"(
#include <metal_stdlib>
using namespace metal;

kernel void apply_lut3d_buffer_to_bgra_texture(
    device const uchar4* inBuffer [[buffer(0)]],
    texture2d<float, access::write> outTexture [[texture(0)]],
    texture3d<float, access::sample> lutTexture [[texture(1)]],
    sampler lutSampler [[sampler(0)]],
    uint2 gid [[thread_position_in_grid]]
) {
    uint width = outTexture.get_width();
    uint height = outTexture.get_height();
    if (gid.x >= width || gid.y >= height) {
        return;
    }

    uint idx = gid.y * width + gid.x;
    uchar4 pixel = inBuffer[idx];
    float3 inColor = float3(pixel.r, pixel.g, pixel.b) / 255.0f;

    // Sample 3D LUT with hardware trilinear / linear filtering
    float4 gradedColor = lutTexture.sample(lutSampler, inColor);
    
    // Write out in RGBA order (Metal BGRA8Unorm automatically maps r->R, g->G, b->B)
    outTexture.write(float4(gradedColor.r, gradedColor.g, gradedColor.b, 1.0f), gid);
}

kernel void passthrough_buffer_to_bgra_texture(
    device const uchar4* inBuffer [[buffer(0)]],
    texture2d<float, access::write> outTexture [[texture(0)]],
    uint2 gid [[thread_position_in_grid]]
) {
    uint width = outTexture.get_width();
    uint height = outTexture.get_height();
    if (gid.x >= width || gid.y >= height) {
        return;
    }

    uint idx = gid.y * width + gid.x;
    uchar4 pixel = inBuffer[idx];
    float3 inColor = float3(pixel.r, pixel.g, pixel.b) / 255.0f;
    outTexture.write(float4(inColor.r, inColor.g, inColor.b, 1.0f), gid);
}
)";

class MetalLutProcessor {
public:
    id<MTLDevice> device = nil;
    id<MTLComputePipelineState> lutPipelineState = nil;
    id<MTLComputePipelineState> passthroughPipelineState = nil;
    id<MTLSamplerState> lutSampler = nil;
    id<MTLTexture> lut3DTexture = nil;
    bool hasLut = false;

    MetalLutProcessor(id<MTLDevice> mtlDevice) : device(mtlDevice) {
        initPipeline();
    }

    bool initPipeline() {
        if (!device) return false;

        NSError* error = nil;
        NSString* shaderSource = [NSString stringWithUTF8String:kMetalLutShaderSource];
        id<MTLLibrary> library = [device newLibraryWithSource:shaderSource options:nil error:&error];
        if (!library) {
            std::cerr << "Metal LUT Shader Compilation Error: " 
                      << (error ? [error.localizedDescription UTF8String] : "Unknown") << std::endl;
            return false;
        }

        id<MTLFunction> lutFunc = [library newFunctionWithName:@"apply_lut3d_buffer_to_bgra_texture"];
        if (lutFunc) {
            lutPipelineState = [device newComputePipelineStateWithFunction:lutFunc error:&error];
        }

        id<MTLFunction> passFunc = [library newFunctionWithName:@"passthrough_buffer_to_bgra_texture"];
        if (passFunc) {
            passthroughPipelineState = [device newComputePipelineStateWithFunction:passFunc error:&error];
        }

        MTLSamplerDescriptor* samplerDesc = [[MTLSamplerDescriptor alloc] init];
        samplerDesc.minFilter = MTLSamplerMinMagFilterLinear;
        samplerDesc.magFilter = MTLSamplerMinMagFilterLinear;
        samplerDesc.mipFilter = MTLSamplerMipFilterLinear;
        samplerDesc.sAddressMode = MTLSamplerAddressModeClampToEdge;
        samplerDesc.tAddressMode = MTLSamplerAddressModeClampToEdge;
        samplerDesc.rAddressMode = MTLSamplerAddressModeClampToEdge;
        samplerDesc.normalizedCoordinates = YES;
        lutSampler = [device newSamplerStateWithDescriptor:samplerDesc];

        return (lutPipelineState != nil && passthroughPipelineState != nil);
    }

    bool loadCubeLut(const std::string& lutPath) {
        if (lutPath.empty()) {
            hasLut = false;
            return true;
        }

        std::ifstream file(lutPath);
        if (!file.is_open()) {
            std::cerr << "Warning: Could not open LUT file at: " << lutPath << std::endl;
            hasLut = false;
            return false;
        }

        int lutSize = 0;
        std::vector<float> tableData;
        std::string line;

        while (std::getline(file, line)) {
            size_t start = line.find_first_not_of(" \t\r\n");
            if (start == std::string::npos) continue;
            line = line.substr(start);

            if (line.empty() || line[0] == '#') continue;

            if (line.find("LUT_3D_SIZE") != std::string::npos) {
                std::istringstream iss(line);
                std::string tag;
                iss >> tag >> lutSize;
                continue;
            }

            if (lutSize > 0) {
                std::istringstream iss(line);
                float r, g, b;
                if (iss >> r >> g >> b) {
                    tableData.push_back(r);
                    tableData.push_back(g);
                    tableData.push_back(b);
                    tableData.push_back(1.0f); // 4-component float texture
                }
            }
        }

        size_t expectedCount = (size_t)lutSize * lutSize * lutSize * 4;
        if (lutSize < 2 || tableData.size() < expectedCount) {
            std::cerr << "Warning: Failed to parse valid 3D .cube LUT data from " << lutPath 
                      << " (size: " << lutSize << ", points: " << tableData.size() / 4 << ")" << std::endl;
            hasLut = false;
            return false;
        }

        MTLTextureDescriptor* desc = [[MTLTextureDescriptor alloc] init];
        desc.textureType = MTLTextureType3D;
        desc.pixelFormat = MTLPixelFormatRGBA32Float;
        desc.width = lutSize;
        desc.height = lutSize;
        desc.depth = lutSize;
        desc.mipmapLevelCount = 1;
        desc.usage = MTLTextureUsageShaderRead;

        lut3DTexture = [device newTextureWithDescriptor:desc];

        [lut3DTexture replaceRegion:MTLRegionMake3D(0, 0, 0, lutSize, lutSize, lutSize)
                        mipmapLevel:0
                              slice:0
                          withBytes:tableData.data()
                        bytesPerRow:lutSize * 4 * sizeof(float)
                      bytesPerImage:lutSize * lutSize * 4 * sizeof(float)];

        hasLut = (lut3DTexture != nil);
        if (hasLut) {
            std::cerr << "Loaded Metal 3D LUT texture (" << lutSize << "^3 grid) from: " << lutPath << std::endl;
        }
        return hasLut;
    }

    void encodeColorGradingFromBuffer(
        id<MTLCommandBuffer> cmdBuf,
        id<MTLBuffer> inBuffer,
        id<MTLTexture> outTexture
    ) {
        if (!cmdBuf || !inBuffer || !outTexture) return;

        id<MTLComputeCommandEncoder> encoder = [cmdBuf computeCommandEncoder];
        if (!encoder) return;

        if (hasLut && lutPipelineState && lut3DTexture && lutSampler) {
            [encoder setComputePipelineState:lutPipelineState];
            [encoder setBuffer:inBuffer offset:0 atIndex:0];
            [encoder setTexture:outTexture atIndex:0];
            [encoder setTexture:lut3DTexture atIndex:1];
            [encoder setSamplerState:lutSampler atIndex:0];
        } else {
            [encoder setComputePipelineState:passthroughPipelineState];
            [encoder setBuffer:inBuffer offset:0 atIndex:0];
            [encoder setTexture:outTexture atIndex:0];
        }

        MTLSize threadgroupSize = MTLSizeMake(16, 16, 1);
        MTLSize threadgroups = MTLSizeMake(
            (outTexture.width + threadgroupSize.width - 1) / threadgroupSize.width,
            (outTexture.height + threadgroupSize.height - 1) / threadgroupSize.height,
            1
        );

        [encoder dispatchThreadgroups:threadgroups threadsPerThreadgroup:threadgroupSize];
        [encoder endEncoding];
    }
};
