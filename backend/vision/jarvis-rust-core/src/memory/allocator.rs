//! Custom memory allocator with alignment support

use std::alloc::{Layout, alloc, dealloc};
use std::ptr::NonNull;
use crate::{Result, JarvisError};

/// Memory alignment options
#[derive(Debug, Clone, Copy)]
pub enum Alignment {
    Byte,       // 1 byte
    Word,       // 4 bytes
    DWord,      // 8 bytes
    Cache,      // 64 bytes (cache line)
    Page,       // 4096 bytes
    Simd128,    // 16 bytes (SIMD 128-bit)
    Simd256,    // 32 bytes (SIMD 256-bit)
}

impl Alignment {
    pub fn as_usize(&self) -> usize {
        match self {
            Alignment::Byte => 1,
            Alignment::Word => 4,
            Alignment::DWord => 8,
            Alignment::Cache => 64,
            Alignment::Page => 4096,
            Alignment::Simd128 => 16,
            Alignment::Simd256 => 32,
        }
    }
}

/// Aligned memory allocator
pub struct AlignedAllocator;

impl AlignedAllocator {
    /// Allocate aligned memory
    pub unsafe fn allocate(size: usize, alignment: Alignment) -> Result<NonNull<u8>> {
        let align = alignment.as_usize();
        
        // Create layout
        let layout = Layout::from_size_align(size, align)
            .map_err(|e| JarvisError::MemoryError(format!("Invalid layout: {}", e)))?;
        
        // Allocate
        let ptr = alloc(layout);
        
        NonNull::new(ptr)
            .ok_or_else(|| JarvisError::MemoryError("Allocation failed".to_string()))
    }
    
    /// Deallocate aligned memory
    pub unsafe fn deallocate(ptr: NonNull<u8>, size: usize, alignment: Alignment) {
        let align = alignment.as_usize();
        let layout = Layout::from_size_align_unchecked(size, align);
        dealloc(ptr.as_ptr(), layout);
    }
}

/// Aligned buffer
pub struct AlignedBuffer {
    ptr: NonNull<u8>,
    size: usize,
    alignment: Alignment,
}

impl AlignedBuffer {
    /// Create new aligned buffer
    pub fn new(size: usize, alignment: Alignment) -> Result<Self> {
        unsafe {
            let ptr = AlignedAllocator::allocate(size, alignment)?;
            Ok(Self { ptr, size, alignment })
        }
    }
    
    /// Get as slice
    pub fn as_slice(&self) -> &[u8] {
        unsafe { std::slice::from_raw_parts(self.ptr.as_ptr(), self.size) }
    }
    
    /// Get as mutable slice
    pub fn as_mut_slice(&mut self) -> &mut [u8] {
        unsafe { std::slice::from_raw_parts_mut(self.ptr.as_ptr(), self.size) }
    }
    
    /// Get pointer
    pub fn as_ptr(&self) -> *const u8 {
        self.ptr.as_ptr()
    }
    
    /// Get mutable pointer
    pub fn as_mut_ptr(&mut self) -> *mut u8 {
        self.ptr.as_ptr()
    }
    
    /// Check alignment
    pub fn is_aligned(&self) -> bool {
        self.ptr.as_ptr() as usize % self.alignment.as_usize() == 0
    }
}

impl Drop for AlignedBuffer {
    fn drop(&mut self) {
        unsafe {
            AlignedAllocator::deallocate(self.ptr, self.size, self.alignment);
        }
    }
}

// SAFETY: `AlignedBuffer` owns its heap allocation exclusively.
// No aliased mutable references exist: `as_mut_ptr` requires `&mut self`.
unsafe impl Send for AlignedBuffer {}
// SAFETY: same invariants as `Send`; no interior mutation API.
unsafe impl Sync for AlignedBuffer {}

#[cfg(test)]
mod tests {
    use super::*;
    
    #[test]
    fn test_aligned_allocation() {
        // Test cache-aligned allocation
        let buffer = AlignedBuffer::new(1024, Alignment::Cache).unwrap();
        assert!(buffer.is_aligned());
        assert_eq!(buffer.as_ptr() as usize % 64, 0);
        
        // Test SIMD-aligned allocation
        let buffer = AlignedBuffer::new(512, Alignment::Simd256).unwrap();
        assert!(buffer.is_aligned());
        assert_eq!(buffer.as_ptr() as usize % 32, 0);
    }
}