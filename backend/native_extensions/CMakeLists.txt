cmake_minimum_required(VERSION 3.12)
project(jarvis_fast_capture)

# C++ standard
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Find Python and pybind11
find_package(Python COMPONENTS Interpreter Development REQUIRED)

# Use pip-installed pybind11
execute_process(
    COMMAND ${Python_EXECUTABLE} -c "import pybind11; print(pybind11.get_cmake_dir())"
    OUTPUT_VARIABLE pybind11_DIR
    OUTPUT_STRIP_TRAILING_WHITESPACE
    ERROR_QUIET
)

if(pybind11_DIR)
    list(APPEND CMAKE_PREFIX_PATH "${pybind11_DIR}")
    find_package(pybind11 CONFIG REQUIRED)
else()
    message(FATAL_ERROR "pybind11 not found. Please install it with: pip install pybind11")
endif()

# macOS specific settings
if(APPLE)
    # Find required frameworks
    find_library(COREGRAPHICS_LIBRARY CoreGraphics)
    find_library(COREFOUNDATION_LIBRARY CoreFoundation)
    find_library(IMAGEIO_LIBRARY ImageIO)
    find_library(APPSERVICES_LIBRARY ApplicationServices)
    find_library(FOUNDATION_LIBRARY Foundation)
    find_library(APPKIT_LIBRARY AppKit)
    find_library(SCREENCAPTUREKIT_LIBRARY ScreenCaptureKit)
    find_library(METAL_LIBRARY Metal)
    find_library(METALKIT_LIBRARY MetalKit)
    find_library(COREVIDEO_LIBRARY CoreVideo)
    find_library(COREMEDIA_LIBRARY CoreMedia)
    find_library(UNIFORMTYPEIDENTIFIERS_LIBRARY UniformTypeIdentifiers)

    set(PLATFORM_LIBRARIES
        ${COREGRAPHICS_LIBRARY}
        ${COREFOUNDATION_LIBRARY}
        ${IMAGEIO_LIBRARY}
        ${APPSERVICES_LIBRARY}
        ${FOUNDATION_LIBRARY}
        ${APPKIT_LIBRARY}
        ${SCREENCAPTUREKIT_LIBRARY}
        ${METAL_LIBRARY}
        ${METALKIT_LIBRARY}
        ${COREVIDEO_LIBRARY}
        ${COREMEDIA_LIBRARY}
        ${UNIFORMTYPEIDENTIFIERS_LIBRARY}
    )

    # Require macOS 12.3+ for ScreenCaptureKit
    set(CMAKE_OSX_DEPLOYMENT_TARGET "12.3" CACHE STRING "Minimum macOS deployment version")
endif()

# Include directories
include_directories(${CMAKE_CURRENT_SOURCE_DIR}/include)

# Source files for fast_capture module
set(FAST_CAPTURE_SOURCES
    src/fast_capture.mm
    src/python_bindings.cpp
)

# Source files for fast_capture_stream module
# Note: Does NOT include fast_capture.mm to avoid duplicate symbols
set(FAST_CAPTURE_STREAM_SOURCES
    src/fast_capture_stream.mm
    src/python_stream_bindings.cpp
)

# Create the Python modules
pybind11_add_module(fast_capture ${FAST_CAPTURE_SOURCES})
pybind11_add_module(fast_capture_stream ${FAST_CAPTURE_STREAM_SOURCES})

# Set source file properties for Objective-C++ (only for .mm files, not during linking)
if(APPLE)
    set_source_files_properties(src/fast_capture.mm PROPERTIES
        COMPILE_FLAGS "-x objective-c++"
    )
    set_source_files_properties(src/fast_capture_stream.mm PROPERTIES
        COMPILE_FLAGS "-x objective-c++"
    )
endif()

# Set properties for fast_capture module
set_target_properties(fast_capture PROPERTIES
    CXX_VISIBILITY_PRESET hidden
    INTERPROCEDURAL_OPTIMIZATION TRUE
    PREFIX "${PYTHON_MODULE_PREFIX}"
    SUFFIX "${PYTHON_MODULE_EXTENSION}"
)

# Set properties for fast_capture_stream module
set_target_properties(fast_capture_stream PROPERTIES
    CXX_VISIBILITY_PRESET hidden
    INTERPROCEDURAL_OPTIMIZATION TRUE
    PREFIX "${PYTHON_MODULE_PREFIX}"
    SUFFIX "${PYTHON_MODULE_EXTENSION}"
)

# Link libraries for both modules
target_link_libraries(fast_capture PRIVATE ${PLATFORM_LIBRARIES})
target_link_libraries(fast_capture_stream PRIVATE ${PLATFORM_LIBRARIES})

# Optimization flags
if(CMAKE_BUILD_TYPE STREQUAL "Release")
    if(APPLE)
        # macOS optimizations for both modules
        target_compile_options(fast_capture PRIVATE
            -O3
            -march=native
            -mtune=native
            -ffast-math
            -funroll-loops
            -ftree-vectorize
        )
        target_compile_options(fast_capture_stream PRIVATE
            -O3
            -march=native
            -mtune=native
            -ffast-math
            -funroll-loops
            -ftree-vectorize
        )
    endif()
endif()

# Install targets
install(TARGETS fast_capture fast_capture_stream
    LIBRARY DESTINATION ${CMAKE_CURRENT_SOURCE_DIR}/../
)