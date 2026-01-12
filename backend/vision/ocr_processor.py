#!/usr/bin/env python3
"""
OCR Processing Module for JARVIS Vision System
Extracts and structures text from screenshots using OCR
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
from datetime import datetime
from PIL import Image
import numpy as np
import pytesseract
import cv2
from concurrent.futures import ThreadPoolExecutor

# Import managed executor for clean shutdown
try:
    from core.thread_manager import ManagedThreadPoolExecutor
    _HAS_MANAGED_EXECUTOR = True
except ImportError:
    _HAS_MANAGED_EXECUTOR = False

import re
import pandas as pd

logger = logging.getLogger(__name__)

@dataclass
class TextRegion:
    """Represents a detected text region"""
    text: str
    confidence: float
    bounding_box: Tuple[int, int, int, int]  # x, y, width, height
    center_point: Tuple[int, int]
    area_type: str  # 'title', 'body', 'button', 'label', 'menu'
    
    @property
    def is_high_confidence(self) -> bool:
        """Check if confidence is high enough for reliable use"""
        return self.confidence > 0.8
        
    def contains_point(self, x: int, y: int) -> bool:
        """Check if a point is within this text region"""
        bbox_x, bbox_y, width, height = self.bounding_box
        return (bbox_x <= x <= bbox_x + width and 
                bbox_y <= y <= bbox_y + height)

@dataclass
class OCRResult:
    """Complete OCR result for an image"""
    timestamp: datetime
    regions: List[TextRegion]
    full_text: str
    processing_time: float
    image_size: Tuple[int, int]
    language: str = 'eng'
    
    def get_text_at_location(self, x: int, y: int, radius: int = 50) -> Optional[str]:
        """Get text near a specific location"""
        for region in self.regions:
            cx, cy = region.center_point
            if abs(cx - x) <= radius and abs(cy - y) <= radius:
                return region.text
        return None
        
    def find_text(self, pattern: str, case_sensitive: bool = False) -> List[TextRegion]:
        """Find regions containing text matching pattern"""
        matching_regions = []
        flags = 0 if case_sensitive else re.IGNORECASE
        
        for region in self.regions:
            if re.search(pattern, region.text, flags):
                matching_regions.append(region)
                
        return matching_regions

class OCRProcessor:
    """Processes images to extract text using OCR"""
    
    def __init__(self, languages: List[str] = None):
        self.languages = languages or ['eng']
        if _HAS_MANAGED_EXECUTOR:

            self.executor = ManagedThreadPoolExecutor(max_workers=2, name='pool')

        else:

            self.executor = ThreadPoolExecutor(max_workers=2)
        
        # OCR configuration
        self.custom_config = '--oem 3 --psm 11'  # Use best OCR engine mode, sparse text
        
        # Text classification patterns
        self.text_patterns = {
            'title': {
                'min_height': 20,
                'max_height': 60,
                'patterns': [r'^[A-Z]', r'^\w+\s*-\s*\w+']
            },
            'button': {
                'patterns': [
                    r'^(OK|Cancel|Submit|Save|Delete|Close|Apply|Next|Back|Continue)$',
                    r'^(Yes|No|Accept|Decline|Confirm)$'
                ],
                'max_words': 3
            },
            'label': {
                'patterns': [r':\s*$', r'^(Name|Email|Password|Username|Date|Time)'],
                'max_words': 5
            },
            'menu': {
                'patterns': [r'^(File|Edit|View|Window|Help|Tools|Format)', r'^\w+\s*>']
            }
        }
        
        # Check if tesseract is available
        try:
            pytesseract.get_tesseract_version()
            self.tesseract_available = True
        except Exception:
            logger.error("Tesseract OCR not found. Please install tesseract-ocr.")
            self.tesseract_available = False
            
    async def process_image(self, image: Image.Image, region: Optional[Tuple[int, int, int, int]] = None) -> OCRResult:
        """Process an image to extract text"""
        start_time = datetime.now()
        
        if not self.tesseract_available:
            return OCRResult(
                timestamp=start_time,
                regions=[],
                full_text="",
                processing_time=0,
                image_size=image.size
            )
            
        # Crop to region if specified
        if region:
            x, y, width, height = region
            image = image.crop((x, y, x + width, y + height))
            
        # Preprocess image
        processed_image = await self._preprocess_image(image)
        
        # Run OCR in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        ocr_data = await loop.run_in_executor(
            self.executor,
            self._perform_ocr,
            processed_image
        )
        
        # Parse OCR results
        regions = self._parse_ocr_data(ocr_data, image.size)
        
        # Extract full text
        full_text = '\n'.join(r.text for r in regions if r.text.strip())
        
        # Calculate processing time
        processing_time = (datetime.now() - start_time).total_seconds()
        
        return OCRResult(
            timestamp=start_time,
            regions=regions,
            full_text=full_text,
            processing_time=processing_time,
            image_size=image.size,
            language='+'.join(self.languages)
        )
        
    async def _preprocess_image(self, image: Image.Image) -> np.ndarray:
        """Preprocess image for better OCR results"""
        # Convert to numpy array
        img_array = np.array(image)
        
        # Convert to grayscale if needed
        if len(img_array.shape) == 3:
            gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
        else:
            gray = img_array
            
        # Apply adaptive thresholding
        thresh = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
            cv2.THRESH_BINARY, 11, 2
        )
        
        # Denoise
        denoised = cv2.fastNlMeansDenoising(thresh, None, 10, 7, 21)
        
        # Optional: Deskew image
        angle = self._get_skew_angle(denoised)
        if abs(angle) > 0.5:
            denoised = self._rotate_image(denoised, angle)
            
        return denoised
        
    def _get_skew_angle(self, image: np.ndarray) -> float:
        """Detect skew angle of text in image"""
        # Find all white pixels
        coords = np.column_stack(np.where(image > 0))
        
        if len(coords) < 100:
            return 0.0
            
        # Use minimum area rectangle
        angle = cv2.minAreaRect(coords)[-1]
        
        if angle < -45:
            angle = -(90 + angle)
        else:
            angle = -angle
            
        return angle
        
    def _rotate_image(self, image: np.ndarray, angle: float) -> np.ndarray:
        """Rotate image by given angle"""
        (h, w) = image.shape[:2]
        center = (w // 2, h // 2)
        
        # Get rotation matrix
        M = cv2.getRotationMatrix2D(center, angle, 1.0)
        
        # Rotate image
        rotated = cv2.warpAffine(
            image, M, (w, h),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE
        )
        
        return rotated
        
    def _perform_ocr(self, image: np.ndarray) -> pd.DataFrame:
        """Perform OCR using Tesseract"""
        try:
            # Get detailed OCR data
            ocr_df = pytesseract.image_to_data(
                image,
                lang='+'.join(self.languages),
                config=self.custom_config,
                output_type=pytesseract.Output.DATAFRAME
            )
            
            return ocr_df
            
        except Exception as e:
            logger.error(f"OCR failed: {e}")
            return pd.DataFrame()
            
    def _parse_ocr_data(self, ocr_data, image_size: Tuple[int, int]) -> List[TextRegion]:
        """Parse OCR data into TextRegion objects"""
        regions = []
        
        if ocr_data.empty:
            return regions
            
        # Group by block for better text grouping
        for block_num in ocr_data['block_num'].unique():
            block_data = ocr_data[ocr_data['block_num'] == block_num]
            
            # Filter out empty text
            text_data = block_data[block_data['text'].notna() & (block_data['text'] != '')]
            
            if text_data.empty:
                continue
                
            # Calculate bounding box for the block
            min_left = text_data['left'].min()
            min_top = text_data['top'].min()
            max_right = (text_data['left'] + text_data['width']).max()
            max_bottom = (text_data['top'] + text_data['height']).max()
            
            # Combine text in block
            block_text = ' '.join(text_data['text'].astype(str))
            
            # Calculate average confidence
            avg_confidence = text_data['conf'].mean() / 100.0
            
            # Skip low confidence blocks
            if avg_confidence < 0.3:
                continue
                
            # Create bounding box
            bbox = (min_left, min_top, max_right - min_left, max_bottom - min_top)
            
            # Calculate center point
            center = (min_left + (max_right - min_left) // 2,
                     min_top + (max_bottom - min_top) // 2)
            
            # Classify text type
            area_type = self._classify_text_type(block_text, bbox, image_size)
            
            regions.append(TextRegion(
                text=block_text.strip(),
                confidence=avg_confidence,
                bounding_box=bbox,
                center_point=center,
                area_type=area_type
            ))
            
        return regions
        
    def _classify_text_type(self, text: str, bbox: Tuple[int, int, int, int], 
                           image_size: Tuple[int, int]) -> str:
        """Classify the type of text region"""
        x, y, width, height = bbox
        img_width, img_height = image_size
        word_count = len(text.split())
        
        # Check button patterns
        for pattern in self.text_patterns['button']['patterns']:
            if re.match(pattern, text.strip(), re.IGNORECASE):
                if word_count <= self.text_patterns['button']['max_words']:
                    return 'button'
                    
        # Check label patterns
        for pattern in self.text_patterns['label']['patterns']:
            if re.search(pattern, text):
                if word_count <= self.text_patterns['label']['max_words']:
                    return 'label'
                    
        # Check menu patterns
        for pattern in self.text_patterns['menu']['patterns']:
            if re.match(pattern, text):
                return 'menu'
                
        # Check if title based on position and size
        if y < img_height * 0.2:  # Top 20% of image
            if height >= self.text_patterns['title']['min_height']:
                if height <= self.text_patterns['title']['max_height']:
                    return 'title'
                    
        # Default to body text
        return 'body'
        
    async def process_screenshot_regions(self, screenshot: Image.Image, 
                                       regions: List[Dict[str, Any]]) -> Dict[str, OCRResult]:
        """Process multiple regions of a screenshot"""
        results = {}
        
        # Process regions in parallel
        tasks = []
        for region_info in regions:
            name = region_info['name']
            bbox = region_info.get('bbox')
            
            task = self.process_image(screenshot, bbox)
            tasks.append((name, task))
            
        # Wait for all OCR tasks
        for name, task in tasks:
            result = await task
            results[name] = result
            
        return results
        
    def extract_structured_data(self, ocr_result: OCRResult) -> Dict[str, Any]:
        """Extract structured data from OCR results"""
        structured = {
            'titles': [],
            'buttons': [],
            'labels': [],
            'menus': [],
            'body_text': [],
            'numbers': [],
            'emails': [],
            'urls': []
        }
        
        # Group by type
        for region in ocr_result.regions:
            if region.area_type == 'title':
                structured['titles'].append(region.text)
            elif region.area_type == 'button':
                structured['buttons'].append({
                    'text': region.text,
                    'location': region.center_point
                })
            elif region.area_type == 'label':
                structured['labels'].append(region.text)
            elif region.area_type == 'menu':
                structured['menus'].append(region.text)
            else:
                structured['body_text'].append(region.text)
                
            # Extract special patterns
            # Numbers
            numbers = re.findall(r'\b\d+(?:\.\d+)?\b', region.text)
            structured['numbers'].extend(numbers)
            
            # Emails
            emails = re.findall(r'\b[\w\.-]+@[\w\.-]+\.\w+\b', region.text)
            structured['emails'].extend(emails)
            
            # URLs
            urls = re.findall(r'https?://\S+', region.text)
            structured['urls'].extend(urls)
            
        return structured

async def test_ocr_processor():
    """Test OCR processor functionality"""
    print("🔤 Testing OCR Processor")
    print("=" * 50)
    
    processor = OCRProcessor()
    
    # Test with a sample image
    try:
        # Create a test image with text
        from PIL import Image, ImageDraw, ImageFont
        
        # Create test image
        img = Image.new('RGB', (800, 600), color='white')
        draw = ImageDraw.Draw(img)
        
        # Try to use a font, fall back to default if not available
        try:
            font_large = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 36)
            font_medium = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 24)
            font_small = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 16)
        except Exception:
            font_large = ImageFont.load_default()
            font_medium = ImageFont.load_default()
            font_small = ImageFont.load_default()
        
        # Add various text elements
        draw.text((50, 50), "Document Title", fill='black', font=font_large)
        draw.text((50, 150), "Name: John Doe", fill='black', font=font_medium)
        draw.text((50, 200), "Email: john.doe@example.com", fill='black', font=font_medium)
        draw.text((50, 300), "This is a sample body text with multiple words.", fill='black', font=font_small)
        draw.text((600, 500), "OK", fill='black', font=font_medium)
        draw.text((700, 500), "Cancel", fill='black', font=font_medium)
        
        # Process the image
        result = await processor.process_image(img)
        
        print(f"\n✅ OCR completed in {result.processing_time:.2f} seconds")
        print(f"📊 Found {len(result.regions)} text regions")
        
        # Show extracted text
        if result.regions:
            print("\n📝 Extracted Text Regions:")
            for region in result.regions:
                print(f"   [{region.area_type}] {region.text} (confidence: {region.confidence:.2f})")
        
        # Extract structured data
        structured = processor.extract_structured_data(result)
        
        print("\n📋 Structured Data:")
        for key, values in structured.items():
            if values:
                print(f"   {key}: {values}")
                
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        
    print("\n✅ OCR processor test complete!")

if __name__ == "__main__":
    asyncio.run(test_ocr_processor())