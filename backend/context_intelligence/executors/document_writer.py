"""
Document Writer Executor
========================

Robust, dynamic document creation with:
- Chrome browser automation (non-incognito)
- Google Docs API for title + content
- Claude API streaming for content generation
- Smart error handling and retry logic
- Real-time progress with dynamic responses
- Zero hardcoding - fully configurable

This module provides comprehensive document creation capabilities with intelligent
narration, async pipeline processing, and support for multiple academic formats.
"""

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional

# Import async pipeline for non-blocking document operations
try:
    from core.async_pipeline import AdvancedAsyncPipeline, get_async_pipeline
except ImportError:
    # Fallback - define stub functions
    def get_async_pipeline():
        return None

    class AdvancedAsyncPipeline:
        pass


logger = logging.getLogger(__name__)


# Formatting templates for different academic styles
FORMAT_SPECIFICATIONS = {
    "mla": {
        "name": "MLA (Modern Language Association)",
        "header": "Last name and page number in upper right corner",
        "title": "Centered, no bold/italics/underline",
        "font": "Times New Roman, 12pt",
        "spacing": "Double-spaced throughout",
        "margins": "1-inch margins on all sides",
        "heading": "Name, instructor, course, date (day Month year) in upper left",
        "citations": "In-text citations with author's last name and page number (Smith 123)",
        "works_cited": "Works Cited page at end, alphabetical by author's last name",
        "paragraphs": "First line indented 0.5 inches",
        "requirements": """
- Times New Roman 12pt font
- Double-spaced throughout (including Works Cited)
- 1-inch margins on all sides
- Header: Your last name and page number (flush right)
- First page heading (flush left): Your Name, Instructor Name, Course, Date
- Title centered (no bold, italics, or underline)
- Indent first line of each paragraph 0.5 inch
- In-text citations: (Author Page#)
- Works Cited page at end with hanging indent""",
    },
    "apa": {
        "name": "APA (American Psychological Association)",
        "header": "Running head on every page (left) and page number (right)",
        "title": "Centered, bold, title case",
        "font": "Times New Roman, 12pt or Calibri, 11pt",
        "spacing": "Double-spaced throughout",
        "margins": "1-inch margins on all sides",
        "heading": "Title page with title, author, institutional affiliation",
        "citations": "In-text citations with author and year (Smith, 2023)",
        "references": "References page at end, alphabetical by author's last name, hanging indent",
        "paragraphs": "First line indented 0.5 inches",
        "requirements": """
- Times New Roman 12pt or Calibri 11pt font
- Double-spaced throughout
- 1-inch margins on all sides
- Running head (shortened title) on all pages, flush left
- Page number on all pages, flush right
- Title page with title (bold, centered), author name, institutional affiliation
- Headings: Level 1 (Centered, Bold), Level 2 (Flush Left, Bold)
- In-text citations: (Author, Year) or (Author, Year, p. #)
- References page with hanging indent, alphabetical order""",
    },
    "chicago": {
        "name": "Chicago/Turabian",
        "header": "Page number in top right or bottom center",
        "title": "Centered, 1/3 down the page",
        "font": "Times New Roman, 12pt",
        "spacing": "Double-spaced",
        "margins": "1-inch margins",
        "heading": "Title page with title, author, course, instructor, date",
        "citations": "Footnotes or endnotes with superscript numbers",
        "bibliography": "Bibliography page at end, alphabetical, hanging indent",
        "paragraphs": "First line indented 0.5 inches",
        "requirements": """
- Times New Roman 12pt font
- Double-spaced (except footnotes/endnotes)
- 1-inch margins
- Title page with title centered 1/3 down page
- Page numbers in header (top right) or footer (bottom center)
- Footnotes or endnotes with full citation on first reference
- Shortened citations for subsequent references
- Bibliography page with hanging indent, alphabetical order""",
    },
    "ieee": {
        "name": "IEEE (Institute of Electrical and Electronics Engineers)",
        "header": "Paper title on left, page number on right",
        "title": "Centered, 18pt Times New Roman",
        "font": "Times New Roman, 10pt",
        "spacing": "Single-spaced",
        "margins": "3/4-inch margins",
        "heading": "Author names centered below title",
        "citations": "Numbered citations in brackets [1]",
        "references": "References numbered in order of appearance",
        "paragraphs": "First line indented 0.25 inches",
        "requirements": """
- Times New Roman 10pt font
- Single-spaced
- Two-column format for most sections
- Title: 18pt, centered
- Author names below title
- Abstract in italics
- In-text citations: numbered [1], [2]
- References numbered in order of appearance""",
    },
    "harvard": {
        "name": "Harvard Referencing",
        "header": "Page number centered at bottom",
        "title": "Centered, bold",
        "font": "Times New Roman or Arial, 12pt",
        "spacing": "1.5 or double-spaced",
        "margins": "1-inch margins",
        "heading": "Title page optional",
        "citations": "In-text: (Author Year) or (Author Year: page)",
        "references": "Reference list at end, alphabetical, hanging indent",
        "paragraphs": "Justified or left-aligned",
        "requirements": """
- Times New Roman or Arial 12pt font
- 1.5 or double-spaced
- 1-inch margins
- In-text citations: (Author Year) or (Author, Year, p.#)
- Reference list at end with hanging indent
- Alphabetical order by author's surname
- Multiple works by same author ordered chronologically""",
    },
    "none": {
        "name": "No specific format",
        "requirements": "Write in a clear, professional manner without specific formatting requirements",
    },
}


class DocumentType(Enum):
    """Supported document types for creation.

    Attributes:
        ESSAY: Academic essay format
        REPORT: Business or technical report
        PAPER: General academic paper
        RESEARCH_PAPER: Research-focused academic paper
        ARTICLE: Article or publication format
        BLOG_POST: Blog post format
        GENERAL: General document type
    """

    ESSAY = "essay"
    REPORT = "report"
    PAPER = "paper"
    RESEARCH_PAPER = "research_paper"
    ARTICLE = "article"
    BLOG_POST = "blog_post"
    GENERAL = "general"


class DocumentFormat(Enum):
    """Academic and professional formatting styles.

    Attributes:
        MLA: Modern Language Association format
        APA: American Psychological Association format
        CHICAGO: Chicago/Turabian format
        IEEE: Institute of Electrical and Electronics Engineers format
        HARVARD: Harvard referencing format
        NONE: No specific formatting requirements
    """

    MLA = "mla"
    APA = "apa"
    CHICAGO = "chicago"
    IEEE = "ieee"
    HARVARD = "harvard"
    NONE = "none"


class DocumentPlatform(Enum):
    """Supported document platforms for creation.

    Attributes:
        GOOGLE_DOCS: Google Docs platform
        WORD_ONLINE: Microsoft Word Online
        LOCAL_WORD: Local Microsoft Word installation
        TEXT_EDITOR: Plain text editor
    """

    GOOGLE_DOCS = "google_docs"
    WORD_ONLINE = "word_online"
    LOCAL_WORD = "local_word"
    TEXT_EDITOR = "text_editor"


@dataclass
class DocumentRequest:
    """Represents a document creation request with all necessary parameters.

    This class encapsulates all the information needed to create a document,
    including content specifications, formatting requirements, and metadata.

    Attributes:
        topic: The main topic or subject of the document
        document_type: Type of document to create (essay, report, etc.)
        word_count: Target word count for the document
        page_count: Target page count for the document
        platform: Platform to create the document on
        formatting: Academic formatting style to use
        browser: Browser to use for document creation
        use_google_docs_api: Whether to use Google Docs API
        claude_model: Claude AI model to use for content generation
        claude_max_tokens: Maximum tokens for Claude API calls
        chunk_size: Size of content chunks for streaming
        stream_delay: Delay between content chunks
        max_retries: Maximum number of retry attempts
        retry_delay: Delay between retry attempts
        title: Document title (auto-generated if empty)
        original_command: Original user command
        additional_requirements: Additional formatting or content requirements
        author_name: Author name for document metadata
        instructor_name: Instructor name for academic formats
        course_name: Course name for academic formats
        institution: Institution name for academic formats
    """

    # Content specs
    topic: str
    document_type: DocumentType
    word_count: Optional[int] = None
    page_count: Optional[int] = None
    platform: DocumentPlatform = DocumentPlatform.GOOGLE_DOCS
    formatting: DocumentFormat = DocumentFormat.NONE

    # Configuration
    browser: str = "Chrome"
    use_google_docs_api: bool = True
    claude_model: str = "claude-3-5-sonnet-20241022"
    claude_max_tokens: int = 4096
    chunk_size: int = 300
    stream_delay: float = 0.3
    max_retries: int = 3
    retry_delay: float = 2.0

    # Metadata
    title: str = ""
    original_command: str = ""
    additional_requirements: str = ""
    author_name: str = "Student Name"
    instructor_name: str = ""
    course_name: str = ""
    institution: str = ""

    def __post_init__(self) -> None:
        """Initialize derived attributes after object creation.

        Generates title and auto-detects format if not provided.
        """
        if not self.title:
            self.title = self._generate_title()

        # Auto-detect format based on document type if not explicitly set
        if self.formatting == DocumentFormat.NONE:
            self.formatting = self._auto_detect_format()

    def _auto_detect_format(self) -> DocumentFormat:
        """Intelligently auto-detect formatting style based on document type.

        Returns:
            DocumentFormat: The most appropriate format for the document type
        """
        # Smart mapping: document type -> most common format
        format_mapping = {
            DocumentType.ESSAY: DocumentFormat.MLA,  # Essays typically use MLA
            DocumentType.RESEARCH_PAPER: DocumentFormat.MLA,  # Academic research papers often use MLA
            DocumentType.PAPER: DocumentFormat.APA,  # General papers often use APA
            DocumentType.REPORT: DocumentFormat.APA,  # Business/scientific reports use APA
            DocumentType.ARTICLE: DocumentFormat.CHICAGO,  # Articles often use Chicago
            DocumentType.BLOG_POST: DocumentFormat.NONE,  # Blog posts are informal
            DocumentType.GENERAL: DocumentFormat.NONE,
        }

        detected = format_mapping.get(self.document_type, DocumentFormat.NONE)
        logger.info(f"Auto-detected format: {detected.value} for {self.document_type.value}")
        return detected

    def _generate_title(self) -> str:
        """Generate intelligent title from topic and document type.

        Returns:
            str: Generated title appropriate for the document type
        """
        topic_words = self.topic.split()
        capitalized = " ".join(word.capitalize() for word in topic_words)

        type_formats = {
            DocumentType.ESSAY: f"{capitalized}: An Essay",
            DocumentType.REPORT: f"Report on {capitalized}",
            DocumentType.PAPER: f"Research Paper: {capitalized}",
            DocumentType.RESEARCH_PAPER: f"{capitalized}",
            DocumentType.ARTICLE: capitalized,
            DocumentType.BLOG_POST: capitalized,
            DocumentType.GENERAL: capitalized,
        }
        return type_formats.get(self.document_type, capitalized)

    def get_length_spec(self) -> str:
        """Get length specification for content generation prompts.

        Returns:
            str: Human-readable length specification
        """
        if self.word_count:
            return f"approximately {self.word_count} words"
        elif self.page_count:
            return f"approximately {self.page_count} pages"

        # Intelligent defaults based on document type
        defaults = {
            DocumentType.ESSAY: "500-750 words",
            DocumentType.REPORT: "1000-1500 words",
            DocumentType.PAPER: "1500-2500 words",
            DocumentType.ARTICLE: "800-1200 words",
            DocumentType.BLOG_POST: "600-1000 words",
        }
        return defaults.get(self.document_type, "750-1000 words")


class DocumentWriterExecutor:
    """Orchestrates document creation workflow with Google Docs API and AI content generation.

    This class manages the complete document creation process, including service initialization,
    document creation, content generation with Claude AI, and real-time progress narration.

    Attributes:
        _google_docs: Google Docs API client
        _claude: Claude AI streaming client
        _browser: Browser automation controller
        _intelligent_narrator: AI-powered narration system
        _speech_in_progress: Flag to prevent overlapping speech
        _speech_queue: Queue for managing speech requests
        _last_speech_end_time: Timestamp of last speech completion
        _narration_context: Context for fallback narration
        _last_narration_time: Timestamp of last narration
        pipeline: Async pipeline for non-blocking operations
    """

    def __init__(self) -> None:
        """Initialize document writer with default configuration."""
        self._google_docs = None
        self._claude = None
        self._browser = None
        self._intelligent_narrator = None  # Will be initialized with Claude

        # Speech queue management to prevent overlapping
        self._speech_in_progress = False
        self._speech_queue = []
        self._last_speech_end_time = 0

        # Fallback context for non-intelligent mode
        self._narration_context = {
            "phase": "initializing",
            "topic": "",
            "progress": 0,
            "current_section": "",
            "word_count": 0,
            "total_words_target": 0,
        }
        self._last_narration_time = 0

        # Initialize async pipeline for non-blocking document operations
        self.pipeline = get_async_pipeline()
        self._register_pipeline_stages()

    def _register_pipeline_stages(self) -> None:
        """Register async pipeline stages for document operations.

        Sets up the pipeline stages for service initialization, document creation,
        content generation, and content streaming with appropriate timeouts and retry logic.
        """
        # Service initialization stage
        self.pipeline.register_stage(
            "service_init", self._init_services_async, timeout=15.0, retry_count=2, required=True
        )

        # Google Doc creation stage
        self.pipeline.register_stage(
            "doc_creation", self._create_doc_async, timeout=20.0, retry_count=3, required=True
        )

        # Content generation stage
        self.pipeline.register_stage(
            "content_generation",
            self._generate_content_async,
            timeout=120.0,  # Longer timeout for AI content generation
            retry_count=1,
            required=True,
        )

        # Content streaming stage
        self.pipeline.register_stage(
            "content_streaming",
            self._stream_to_doc_async,
            timeout=60.0,
            retry_count=1,
            required=True,
        )

    async def _init_services_async(self, context) -> None:
        """Non-blocking service initialization via async pipeline.

        Args:
            context: Pipeline context containing request metadata

        Raises:
            Exception: If service initialization fails
        """
        request = context.metadata.get("request")

        try:
            # Initialize Google Docs API
            if request.use_google_docs_api:
                from ..automation.google_docs_api import get_google_docs_client

                self._google_docs = get_google_docs_client()

                if not await self._google_docs.authenticate():
                    context.metadata["error"] = "Failed to authenticate with Google Docs API"
                    return

            # Initialize Claude
            from ..automation.claude_streamer import get_claude_streamer

            self._claude = get_claude_streamer()

            # Initialize browser
            from ..automation.browser_controller import get_browser_controller

            self._browser = get_browser_controller(request.browser)

            context.metadata["services_ready"] = True

        except Exception as e:
            logger.error(f"Error initializing services: {e}")
            context.metadata["error"] = str(e)

    async def _create_doc_async(self, context) -> None:
        """Non-blocking Google Doc creation via async pipeline.

        Args:
            context: Pipeline context containing request metadata
        """
        request = context.metadata.get("request")

        for attempt in range(request.max_retries):
            try:
                doc_info = await self._google_docs.create_document(request.title)
                if doc_info:
                    logger.info(f"Created Google Doc: {doc_info['document_id']}")
                    context.metadata["doc_info"] = doc_info
                    context.metadata["document_id"] = doc_info["document_id"]
                    context.metadata["document_url"] = doc_info["document_url"]
                    return
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1} failed: {e}")
                if attempt < request.max_retries - 1:
                    await asyncio.sleep(request.retry_delay)

        context.metadata["error"] = "Failed to create Google Doc after retries"

    async def _generate_content_async(self, context) -> None:
        """Non-blocking content generation via async pipeline.

        Args:
            context: Pipeline context containing request and outline metadata
        """
        request = context.metadata.get("request")
        outline = context.metadata.get("outline")

        content_prompt = self._build_content_prompt(request, outline)
        context.metadata["content_prompt"] = content_prompt
        context.metadata["ready_for_streaming"] = True

    async def _stream_to_doc_async(self, context) -> None:
        """Non-blocking content streaming to Google Doc via async pipeline.

        Args:
            context: Pipeline context containing document and streaming metadata
        """
        document_id = context.metadata.get("document_id")
        request = context.metadata.get("request")
        content_prompt = context.metadata.get("content_prompt")
        context.metadata.get("progress_callback")
        context.metadata.get("websocket")

        word_count = 0
        buffer = ""

        try:
            async for chunk in self._claude.stream_content(
                content_prompt, max_tokens=request.claude_max_tokens, model=request.claude_model
            ):
                buffer += chunk

                if len(buffer) >= request.chunk_size:
                    success = await self._google_docs.append_text(document_id, buffer)

                    if success:
                        current_words = len(buffer.split())
                        word_count += current_words

                    buffer = ""
                    await asyncio.sleep(request.stream_delay)

            # Write remaining buffer
            if buffer:
                await self._google_docs.append_text(document_id, buffer)
                word_count += len(buffer.split())

            context.metadata["word_count"] = word_count

        except Exception as e:
            logger.error(f"Error streaming content: {e}")
            context.metadata["error"] = str(e)
            context.metadata["word_count"] = word_count

    async def create_document(
        self, request: DocumentRequest, progress_callback: Optional[Callable] = None, websocket=None
    ) -> Dict[str, Any]:
        """Execute end-to-end document creation with detailed real-time communication.

        This is the main entry point for document creation. It orchestrates the entire
        process from service initialization to final document completion with real-time
        progress updates and intelligent narration.

        Args:
            request: Document creation request with all specifications
            progress_callback: Optional callback function for progress updates
            websocket: Optional WebSocket connection for real-time updates

        Returns:
            Dict containing:
                - success: Boolean indicating if creation was successful
                - document_id: Google Docs document ID (if successful)
                - document_url: URL to the created document (if successful)
                - title: Document title
                - word_count: Final word count
                - platform: Platform used for creation
                - topic: Document topic
                - formatting: Formatting style used
                - error: Error message (if unsuccessful)

        Raises:
            Exception: If critical errors occur during document creation

        Example:
            >>> request = DocumentRequest(
            ...     topic="Climate Change",
            ...     document_type=DocumentType.ESSAY,
            ...     word_count=1000
            ... )
            >>> result = await executor.create_document(request)
            >>> print(result['success'])
            True
        """
        try:
            # Initialize narration context
            self._narration_context = {
                "phase": "starting",
                "topic": request.topic,
                "document_type": request.document_type.value,
                "format": request.formatting.value,
                "progress": 0,
                "word_count": 0,
                "total_words_target": request.word_count or 1000,
                "current_section": "initialization",
            }

            # Phase 1: Dynamic announcement
            await self._narrate(
                progress_callback,
                websocket,
                context={
                    "phase": "acknowledging_request",
                    "topic": request.topic,
                    "document_type": request.document_type.value,
                    "format": request.formatting.value,
                    "total_words_target": request.word_count or 1000,
                },
            )

            # Phase 2: Initialize services via async pipeline
            await self._narrate(
                progress_callback,
                websocket,
                context={"phase": "initializing_services", "progress": 10},
            )

            # Route through async pipeline for service initialization
            init_result = await self.pipeline.process_async(
                text=f"Initialize document services for {request.topic}",
                metadata={"request": request, "stage": "service_init"},
            )

            if init_result.get("metadata", {}).get("error"):
                return {"success": False, "error": init_result["metadata"]["error"]}

            await self._narrate(
                progress_callback, websocket, context={"phase": "services_ready", "progress": 20}
            )

            # Phase 3: Create Google Doc via async pipeline
            await self._narrate(
                progress_callback,
                websocket,
                context={
                    "phase": "creating_document",
                    "progress": 25,
                    "current_section": f"Google Doc: {request.title}",
                },
            )

            # Route through async pipeline for doc creation
            doc_result = await self.pipeline.process_async(
                text=f"Create Google Doc: {request.title}",
                metadata={"request": request, "stage": "doc_creation"},
            )

            doc_metadata = doc_result.get("metadata", {})
            if doc_metadata.get("error"):
                return {"success": False, "error": doc_metadata["error"]}

            document_id = doc_metadata.get("document_id")
            document_url = doc_metadata.get("document_url")

            if not document_id or not document_url:
                return {"success": False, "error": "Failed to create Google Doc"}

            await self._narrate(
                progress_callback, websocket, context={"phase": "document_created", "progress": 30}
            )

            # Phase 4: Open in Chrome
            await self._narrate(
                progress_callback, websocket, context={"phase": "opening_browser", "progress": 35}
            )

            await self._open_in_browser(document_url, request)
            await asyncio.sleep(1.5)

            await self._narrate(
                progress_callback, websocket, context={"phase": "browser_ready", "progress": 40}
            )

            # Phase 5: Generate outline with detail
            await self._narrate(
                progress_callback,
                websocket,
                context={
                    "phase": "analyzing_topic",
                    "progress": 45,
                    "current_section": "outline generation",
                },
            )

            outline = await self._generate_outline(request)

            section_count = len(outline.get("sections", []))
            sections = outline.get("sections", [])
            section_names = ", ".join([s["name"] for s in sections])

            await self._narrate(
                progress_callback,
                websocket,
                context={
                    "phase": "outline_complete",
                    "progress": 50,
                    "section_count": section_count,
                    "sections": section_names,
                },
            )

            # Phase 6: Stream content with detailed progress
            await self._narrate(
                progress_callback,
                websocket,
                context={
                    "phase": "starting_writing",
                    "progress": 55,
                    "current_section": sections[0]["name"] if sections else "introduction",
                },
            )

            word_count = await self._stream_content(
                document_id, request, outline, progress_callback, websocket
            )

            # Phase 7: Completion with summary - CRITICAL announcements
            await self._narrate(
                progress_callback,
                websocket,
                context={
                    "phase": "writing_complete",
                    "progress": 95,
                    "word_count": word_count,
                    "topic": request.topic,
                    "document_type": request.document_type.value,
                },
            )

            # Small delay for natural pacing between completion messages
            await asyncio.sleep(1.5)

            await self._narrate(
                progress_callback,
                websocket,
                context={
                    "phase": "document_ready",
                    "progress": 100,
                    "word_count": word_count,
                    "section_count": section_count,
                    "topic": request.topic,
                    "document_type": request.document_type.value,
                },
            )

            return {
                "success": True,
                "document_id": document_id,
                "document_url": document_url,
                "title": request.title,
                "word_count": word_count,
                "platform": request.platform.value,
                "topic": request.topic,
                "formatting": request.formatting.value,
            }

        except Exception as e:
            logger.error(f"Error in document creation: {e}", exc_info=True)
            await self._narrate(
                progress_callback, websocket, f"I apologize, Sir. I encountered an error: {str(e)}"
            )
            await self._narrate(
                progress_callback, websocket, "I'll attempt to recover and continue..."
            )
            return {"success": False, "error": str(e)}

    async def _initialize_services(self, request: DocumentRequest) -> bool:
        """Initialize required services for document creation.

        Args:
            request: Document request containing service configuration

        Returns:
            bool: True if all services initialized successfully, False otherwise
        """
        try:
            # Initialize Google Docs API
            if request.use_google_docs_api:
                from ..automation.google_docs_api import get_google_docs_client

                self._google_docs = get_google_docs_client()

                if not await self._google_docs.authenticate():
                    logger.error("Failed to authenticate with Google Docs API")
                    return False

            # Initialize Claude
            from ..automation.claude_streamer import get_claude_streamer

            self._claude = get_claude_streamer()

            # Initialize Intelligent Narrator with Claude
            from .intelligent_narrator import get_intelligent_narrator

            self._intelligent_narrator = get_intelligent_narrator(self._claude)
            await self._intelligent_narrator.initialize(
                topic=request.topic,
                doc_type=request.document_type.value,
                format_style=request.formatting.value,
                target_words=request.word_count or 1000,
                claude_client=self._claude,
            )
            logger.info("[DOCUMENT WRITER] ✅ Intelligent Narrator initialized")

            # Initialize browser
            from ..automation.browser_controller import get_browser_controller

            self._browser = get_browser_controller(request.browser)

            return True

        except Exception as e:
            logger.error(f"Error initializing services: {e}")
            return False

    async def _create_google_doc(self, request: DocumentRequest) -> Optional[Dict[str, Any]]:
        """Create Google Doc via API with retry logic.

        Args:
            request: Document request containing title and retry configuration

        Returns:
            Optional[Dict[str, Any]]: Document info dict with ID and URL, or None if failed
        """
        for attempt in range(request.max_retries):
            try:
                doc_info = await self._google_docs.create_document(request.title)
                if doc_info:
                    logger.info(f"Created Google Doc: {doc_info['document_id']}")
                    return doc_info
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1} failed: {e}")
                if attempt < request.max_retries - 1:
                    await asyncio.sleep(request.retry_delay)
        return None

    async def _open_in_browser(self, url: str, request: DocumentRequest) -> bool:
        """Open document in browser.

        Args:
            url: Document URL to open
            request: Document request containing browser configuration

        Returns:
            bool: True if successfully opened, False otherwise
        """
        try:
            success = await self._browser.navigate(url)
            if success:
                logger.info(f"Opened document in {request.browser}")
            return success
        except Exception as e:
            logger.error(f"Error opening in browser: {e}")
            return False

    async def _generate_outline(self, request: DocumentRequest) -> Dict[str, Any]:
        """Generate document outline using Claude AI.

        Args:
            request: Document request containing topic and requirements

        Returns:
            Dict[str, Any]: Outline structure with title and sections
        """
        article = "an" if request.document_type.value[0] in "aeiou" else "a"
        outline_prompt = f"""Create a detailed outline for {article} {request.document_type.value} about "{request.topic}".

Target length: {request.target_length} words
Style: {request.style}

Return a JSON structure with:
- title: Document title
- sections: List of section objects with title and bullet points
"""
        # TODO: Implement outline generation using Claude API
        return {"title": request.topic, "sections": []}

    async def _narrate(self, progress_callback: Optional[Callable],
                      websocket, message: str = None, context: Dict[str, Any] = None):
        """
        Intelligent narration with AI-powered decision making and speech queue management
        Uses IntelligentNarrator for dynamic, context-aware updates
        """
        import time
        import asyncio

        # CRITICAL PHASES: Completion messages MUST always be heard
        critical_phases = ['writing_complete', 'document_ready', 'acknowledging_request']
        is_critical = context and context.get('phase') in critical_phases
        
        if is_critical:
            logger.info(f"[DOCUMENT WRITER] 🎯 CRITICAL PHASE: {context.get('phase')} - Will announce regardless")
            # Wait for any in-progress speech to finish for critical messages
            if self._speech_in_progress:
                logger.info(f"[DOCUMENT WRITER] ⏳ Waiting for speech to complete before critical announcement...")
                max_wait = 10  # Wait up to 10 seconds
                waited = 0
                while self._speech_in_progress and waited < max_wait:
                    await asyncio.sleep(0.5)
                    waited += 0.5
                logger.info(f"[DOCUMENT WRITER] ✅ Speech clear, proceeding with critical announcement")
        else:
            # REGULAR PHASES: Wait if speech is currently in progress to prevent overlap
            if self._speech_in_progress:
                logger.info(f"[DOCUMENT WRITER] ⏸️  Speech in progress, skipping to prevent overlap")
                return
            
            # Wait for minimum gap after last speech completed
            time_since_last_speech = time.time() - self._last_speech_end_time
            if time_since_last_speech < 2.0 and self._last_speech_end_time > 0:
                logger.info(f"[DOCUMENT WRITER] ⏸️  Too soon after last speech ({time_since_last_speech:.1f}s), skipping")
                return

        # If intelligent narrator is available, use it
        if self._intelligent_narrator and context and not is_critical:
            # Regular phases use intelligent narrator with significance checks
            phase = context.get('phase', 'unknown')
            
            # Update narrator context
            self._intelligent_narrator._context.current_phase = phase
            self._intelligent_narrator._context.current_section = context.get('current_section', '')
            self._intelligent_narrator._context.word_count = context.get('word_count', 0)
            
            # Intelligently decide if we should narrate
            should_narrate, reason = await self._intelligent_narrator.should_narrate(
                phase, 
                content_update=context.get('recent_content')
            )
            
            if not should_narrate:
                logger.info(f"[INTELLIGENT NARRATOR] ⏭️  Skipping: {reason}")
                return
            
            logger.info(f"[INTELLIGENT NARRATOR] 🎯 Narrating: {reason}")
            
            # Generate intelligent, context-aware message
            try:
                message = await self._intelligent_narrator.generate_narration(phase, context)
            except Exception as e:
                logger.error(f"[INTELLIGENT NARRATOR] Error, using fallback: {e}")
                message = await self._generate_dynamic_narration(context)
        
        # CRITICAL phases bypass intelligent narrator and use guaranteed messages
        elif is_critical and context:
            logger.info(f"[DOCUMENT WRITER] 🔥 Generating CRITICAL message for {context.get('phase')}")
            message = await self._generate_dynamic_narration(context)
        
        # Fallback to old system if no intelligent narrator
        elif context and not message:
            message = await self._generate_dynamic_narration(context)
        
        if not message:
            return

        # Mark speech as in progress
        self._speech_in_progress = True
        logger.info(f"[DOCUMENT WRITER] 🎤 Speaking: {message}")

        if progress_callback:
            await progress_callback(message)

        if websocket:
            try:
                # Send narration message with voice output
                await websocket.send_json({
                    "type": "voice_narration",
                    "message": message,
                    "speak": True
                })
                
                # Estimate speech duration (rough: ~3 words per second + 1s buffer)
                word_count = len(message.split())
                estimated_duration = (word_count / 3.0) + 1.0
                
                # Wait for speech to complete
                await asyncio.sleep(estimated_duration)
                
                # Mark speech as complete
                self._speech_in_progress = False
                self._last_speech_end_time = time.time()
                self._last_narration_time = time.time()
                
                logger.info(f"[DOCUMENT WRITER] ✅ Speech completed ({estimated_duration:.1f}s)")

            except Exception as e:
                logger.error(f"Could not send narration to websocket: {e}")
                self._speech_in_progress = False

    def _get_fallback_narration(self, context_override: Dict[str, Any] = None) -> str:
        """Generate intelligent, dynamic fallback narration when Claude is not available"""
        import random
        
        # Use override context if provided, otherwise use stored context
        ctx = context_override if context_override else self._narration_context
        
        phase = ctx.get('phase', 'working')
        topic = ctx.get('topic', 'your document')
        progress = ctx.get('progress', 0)
        current_section = ctx.get('current_section', 'the document')
        word_count = ctx.get('word_count', 0)
        doc_type = ctx.get('document_type', 'document')
        
        # Dynamic phrase components for natural variation
        acknowledgments = ["Got it", "Understood", "Absolutely", "Right away", "On it", "Let's begin", "Starting now"]
        transitions = ["Moving on to", "Now working on", "Progressing to", "Shifting to", "Starting", "Beginning"]
        progress_phrases = ["We're at", "Progress update:", "Currently at", "Now at", "Reached"]
        completion_phrases = ["All done", "Finished", "Complete", "Ready", "Done"]
        
        # Occasionally use "Sir" (20-30% chance)
        use_sir = random.random() < 0.25
        sir_phrase = ", Sir" if use_sir else ""
        
        # Generate natural, varied responses based on phase
        if phase == 'acknowledging_request':
            intros = [
                f"{random.choice(acknowledgments)}{sir_phrase}. Creating your {doc_type} about {topic}",
                f"I'll write that {doc_type} on {topic} for you{sir_phrase}",
                f"{topic} - interesting topic. Let me create that {doc_type}",
                f"Starting your {doc_type} about {topic} now"
            ]
            return random.choice(intros)
            
        elif phase == 'initializing_services':
            return random.choice([
                "Connecting to Google Docs",
                "Setting up document services",
                "Initializing the writing system",
                f"Preparing to write{sir_phrase}",
                "Getting everything ready",
                "Spinning up the document tools"
            ])
            
        elif phase == 'services_ready':
            return random.choice([
                "Services connected",
                "Ready to create the document",
                "All systems go",
                f"Tools initialized{sir_phrase}"
            ])
            
        elif phase == 'creating_document':
            return random.choice([
                "Creating your document in Google Docs",
                "Setting up the document structure",
                f"Opening a new {doc_type} for you",
                "Establishing document framework",
                "Building the document"
            ])
            
        elif phase == 'document_created':
            return random.choice([
                "Document created successfully",
                "Got your new document ready",
                f"Fresh document set up{sir_phrase}",
                "Document structure in place"
            ])
            
        elif phase == 'opening_browser':
            return random.choice([
                "Opening in Chrome",
                "Launching the browser",
                "Bringing up Google Docs"
            ])
            
        elif phase == 'browser_ready':
            return random.choice([
                "Browser's open and ready",
                "Document visible on screen",
                "Chrome has it loaded"
            ])
            
        elif phase == 'analyzing_topic':
            analyses = [
                f"Analyzing key aspects of {topic}",
                f"Researching {topic} for comprehensive coverage",
                f"Identifying main themes about {topic}",
                f"Structuring thoughts on {topic}",
                f"Planning the approach to {topic}",
                f"Mapping out {topic} coverage"
            ]
            return random.choice(analyses)
            
        elif phase == 'outline_complete':
            return random.choice([
                f"Outline ready - found several interesting angles",
                "Structure mapped out, ready to write",
                f"Framework complete{sir_phrase}",
                "Got the blueprint, starting content",
                "Outline looking solid",
                "Plan's in place, let's write"
            ])
            
        elif phase == 'starting_writing':
            return random.choice([
                f"Writing about {topic} now",
                "Composing the content",
                f"Let me craft this {doc_type} for you{sir_phrase}",
                "Beginning the actual writing",
                f"Starting with the introduction about {topic}",
                f"Getting the words down now"
            ])
            
        elif phase == 'writing_section':
            section_updates = [
                f"{random.choice(transitions)} {current_section}",
                f"Writing {current_section}",
                f"Developing {current_section} now",
                f"Crafting {current_section}",
                f"{current_section} coming together nicely",
                f"Building out {current_section}",
                f"Now covering {current_section}"
            ]
            return random.choice(section_updates)
            
        elif phase == 'progress_update':
            # More varied progress announcements with specific milestones
            if progress < 20:
                stage_phrases = [
                    "Just getting started",
                    "Opening strong",
                    "Building momentum",
                    "Laying the groundwork"
                ]
            elif progress < 40:
                stage_phrases = [
                    "Making solid progress",
                    "Coming along nicely",
                    "Building the argument",
                    "Developing the ideas"
                ]
            elif progress < 60:
                stage_phrases = [
                    "Halfway there",
                    "Making great headway",
                    "Rolling through this",
                    "Really cooking now"
                ]
            elif progress < 80:
                stage_phrases = [
                    "Into the home stretch",
                    "Getting close",
                    "Nearly there",
                    "Closing in on completion"
                ]
            else:
                stage_phrases = [
                    "Almost finished",
                    "Just about done",
                    "Final stretch",
                    "Wrapping it up"
                ]
                
            progress_msgs = [
                f"{random.choice(stage_phrases)} - {word_count} words",
                f"{word_count} words written, {random.choice(stage_phrases).lower()}",
                f"{random.choice(progress_phrases)} {progress}%{sir_phrase}",
                f"{random.choice(stage_phrases)}"
            ]
            return random.choice(progress_msgs)
            
        elif phase == 'finalizing':
            return random.choice([
                "Adding final touches",
                "Wrapping up the conclusion",
                "Polishing the final sections",
                f"Nearly finished{sir_phrase}"
            ])
            
        elif phase == 'writing_complete':
            completions = [
                f"All done{sir_phrase}! {word_count} words on {topic}",
                f"Finished! Your {doc_type} about {topic} is complete - {word_count} words",
                f"Writing complete{sir_phrase}. {word_count} words on {topic}",
                f"That's {word_count} words finished on {topic}",
                f"Complete! Your essay on {topic} is ready - {word_count} words total"
            ]
            return random.choice(completions)
            
        elif phase == 'document_ready':
            ready_msgs = [
                f"Your {doc_type} about {topic} is open in Google Docs{sir_phrase}",
                f"It's ready for you in the browser - all {word_count} words",
                f"Document's on your screen now{sir_phrase}",
                f"There you go{sir_phrase} - {topic} essay ready to review",
                f"All set - your {doc_type} on {topic} is open and ready"
            ]
            return random.choice(ready_msgs)
            
        else:
            # Default fallback with variation
            return random.choice([
                f"Making progress on {current_section}",
                f"Continuing with {current_section}",
                f"Still writing{sir_phrase}",
                f"Moving through {current_section}"
            ])

    async def _generate_dynamic_narration(self, context_update: Dict[str, Any]) -> str:
        """
        Generate natural, context-aware narration using Claude API.
        No hardcoded messages - all narration is dynamically generated.
        """
        # Update context
        self._narration_context.update(context_update)

        # Build prompt for Claude to generate natural narration
        prompt = f"""You are JARVIS, Tony Stark's AI assistant. You're helping write a {self._narration_context.get('document_type', 'document')} about "{self._narration_context.get('topic', 'the topic')}".

Current status:
- Phase: {self._narration_context.get('phase')}
- Progress: {self._narration_context.get('progress')}% complete
- Word count: {self._narration_context.get('word_count')} / {self._narration_context.get('total_words_target')} words
- Current section: {self._narration_context.get('current_section', 'N/A')}
- Format: {self._narration_context.get('format', 'standard')}

Generate a single, natural sentence (10-15 words) that JARVIS would say to update the user about this progress. 
Be conversational and natural. Only occasionally use "Sir" (maybe 20% of the time). Vary your language - don't be repetitive.
Reference specific details when relevant. Sound engaged and interested in the topic.

Narration:"""

        try:
            # Try to use dynamic response generator first for consistent personality
            try:
                from voice.dynamic_response_generator import get_response_generator
                generator = get_response_generator()
                narration = generator.generate_document_narration(
                    self._narration_context.get('phase'),
                    self._narration_context
                )
                if narration:
                    return narration
            except Exception as e:
                logger.debug(f"Dynamic generator not available: {e}")
            
            # Use Claude to generate the narration if available
            if self._claude:
                response = ""
                async for chunk in self._claude.stream_content(prompt, max_tokens=50):
                    response += chunk

                # Clean up the response
                narration = response.strip().strip('"').strip()
                return narration if narration else self._get_fallback_narration(context_update)
            else:
                # Fallback if Claude not available
                return self._get_fallback_narration(context_update)
        except Exception as e:
            logger.error(f"Error generating dynamic narration: {e}")
            return self._get_fallback_narration(context_update)

    def _format_outline(self, outline: Dict[str, Any]) -> str:
        """Format outline for prompt"""
        lines = []
        for section in outline.get('sections', []):
            lines.append(f"- {section['name']}")
            for point in section.get('points', []):
                lines.append(f"  * {point}")
        return '\n'.join(lines)

    def _build_content_prompt(self, request: DocumentRequest, outline: Dict[str, Any]) -> str:
        """Build comprehensive prompt for content generation with formatting"""
        # Get formatting specifications
        format_spec = FORMAT_SPECIFICATIONS.get(request.formatting.value, {})
        format_name = format_spec.get("name", "No specific format")
        format_requirements = format_spec.get("requirements", "")

        # Build heading information if applicable
        heading_info = ""
        if request.formatting == DocumentFormat.MLA:
            heading_info = f"""
MLA Heading (upper left corner):
{request.author_name}
{request.instructor_name if request.instructor_name else "Instructor Name"}
{request.course_name if request.course_name else "Course Name"}
{datetime.now().strftime("%d %B %Y")}
"""
        elif request.formatting == DocumentFormat.APA:
            heading_info = f"""
Title Page:
Title: {request.title}
Author: {request.author_name}
Institutional Affiliation: {request.institution if request.institution else "Institution Name"}
"""

        prompt = f"""Write a complete, high-quality {request.document_type.value} about "{request.topic}" in {format_name} format.

Target length: {request.get_length_spec()}

Title: {request.title}

FORMATTING REQUIREMENTS - {format_name}:
{format_requirements}

{heading_info}

Outline:
{self._format_outline(outline)}

Content Requirements:
- Professional, well-researched content
- Clear structure with proper transitions
- Engaging and informative writing
- Academic/professional tone appropriate for a {request.document_type.value}
- STRICTLY follow {format_name} formatting guidelines above
- Include proper in-text citations in {format_name} style (use plausible sources)
- Include a proper Works Cited/References page at the end in {format_name} format
{f"- {request.additional_requirements}" if request.additional_requirements else ""}

IMPORTANT: Format the entire document according to {format_name} standards, including:
- Proper heading/title page
- Correct spacing and indentation
- Appropriate in-text citations
- Properly formatted Works Cited/References page

Write the complete {request.document_type.value} now, starting with the proper {format_name} heading:"""

        return prompt

    async def _stream_content(self,
                            document_id: str,
                            request: DocumentRequest,
                            outline: Dict[str, Any],
                            progress_callback: Optional[Callable],
                            websocket) -> int:
        """Stream content to Google Doc via API with detailed real-time updates"""
        import time
        content_prompt = self._build_content_prompt(request, outline)

        word_count = 0
        sentence_count = 0
        buffer = ""
        progress_interval = 200  # Update every 200 words to avoid over-narration
        next_milestone = progress_interval
        
        # Track time for velocity calculation
        last_write_time = time.time()

        # Track sections for progress updates
        sections = outline.get('sections', [])
        current_section_index = 0
        section_announced = False

        try:
            async for chunk in self._claude.stream_content(
                content_prompt,
                max_tokens=request.claude_max_tokens,
                model=request.claude_model
            ):
                buffer += chunk

                # Count sentences for more granular updates
                sentence_count += buffer.count('.') + buffer.count('!') + buffer.count('?')

                # Write in smaller chunks for more real-time feel
                if len(buffer) >= request.chunk_size:
                    success = await self._google_docs.append_text(document_id, buffer)

                    if success:
                        current_words = len(buffer.split())
                        current_time = time.time()
                        time_delta = current_time - last_write_time
                        word_count += current_words
                        
                        # Update intelligent narrator metrics
                        if self._intelligent_narrator:
                            self._intelligent_narrator.update_writing_metrics(word_count, time_delta)
                            self._intelligent_narrator.update_content_analysis(buffer)
                        
                        last_write_time = current_time

                        # Detect section changes (simple heuristic)
                        if current_section_index < len(sections) and not section_announced:
                            section_name = sections[current_section_index]['name']
                            # Announce every section for better engagement
                            await self._narrate(progress_callback, websocket, context={
                                "phase": "writing_section",
                                "current_section": section_name,
                                "word_count": word_count,
                                "progress": 55 + int((word_count / (request.word_count or 1000)) * 40),
                                "recent_content": buffer[:200]  # Pass snippet for context
                            })
                            section_announced = True

                        # Move to next section periodically (every 200 words to match progress updates)
                        if word_count > (current_section_index + 1) * 200 and current_section_index < len(sections) - 1:
                            current_section_index += 1
                            section_announced = False

                        # Progress milestones with dynamic narration
                        if word_count >= next_milestone:
                            percentage = min(int((word_count / (request.word_count or 1000)) * 100), 99)
                            await self._narrate(progress_callback, websocket, context={
                                "phase": "progress_update",
                                "word_count": word_count,
                                "progress": 55 + int(percentage * 0.4),
                                "current_section": sections[current_section_index]['name'] if current_section_index < len(sections) else "conclusion",
                                "recent_content": buffer[:200]  # Pass snippet for context
                            })
                            next_milestone += progress_interval

                    buffer = ""
                    await asyncio.sleep(request.stream_delay)

            # Write remaining buffer
            if buffer:
                await self._google_docs.append_text(document_id, buffer)
                word_count += len(buffer.split())

            # Final section notification
            if current_section_index < len(sections):
                await self._narrate(progress_callback, websocket, context={
                    "phase": "finalizing",
                    "current_section": "conclusion and references",
                    "word_count": word_count,
                    "progress": 90
                })

            return word_count

        except Exception as e:
            logger.error(f"Error streaming content: {e}")
            await self._narrate(progress_callback, websocket,
                f"Encountered an issue during writing, but continuing... ({word_count} words so far)")
            return word_count


# ============================================================================
# SINGLETON PATTERN - Global accessor
# ============================================================================

_document_writer_instance: Optional[DocumentWriterExecutor] = None


def parse_document_request(command: str) -> Optional[DocumentRequest]:
    """
    Parse a natural language command into a DocumentRequest.

    Args:
        command: Natural language command

    Returns:
        DocumentRequest if parseable, None otherwise
    """
    command_lower = command.lower()

    # Simple pattern matching for document requests
    if any(word in command_lower for word in ["write", "create", "draft", "compose"]):
        # Extract topic (very basic - could be improved with NLP)
        topic = command.replace("write", "").replace("create", "").replace("draft", "").replace("compose", "").strip()
        topic = topic.replace("a ", "").replace("an ", "").replace("about ", "").replace("on ", "").strip()

        # Determine document type
        doc_type = DocumentType.DOCUMENT  # Default
        if "essay" in command_lower:
            doc_type = DocumentType.ESSAY
        elif "report" in command_lower:
            doc_type = DocumentType.REPORT
        elif "email" in command_lower:
            doc_type = DocumentType.EMAIL
        elif "letter" in command_lower:
            doc_type = DocumentType.LETTER

        if topic:
            return DocumentRequest(
                topic=topic,
                document_type=doc_type,
                target_length=1000,  # Default
                style="Professional",
            )

    return None


def get_document_writer() -> DocumentWriterExecutor:
    """
    Get or create the global DocumentWriterExecutor instance.

    Returns:
        DocumentWriterExecutor: The singleton instance

    Example:
        writer = get_document_writer()
        result = await writer.execute(request)
    """
    global _document_writer_instance

    if _document_writer_instance is None:
        _document_writer_instance = DocumentWriterExecutor()
        logger.info("Created DocumentWriterExecutor singleton instance")

    return _document_writer_instance


# ============================================================================
# TEST FUNCTION
# ============================================================================


async def test_document_writer():
    """Test document writer functionality."""
    writer = get_document_writer()

    request = DocumentRequest(
        topic="The Impact of AI on Education",
        document_type=DocumentType.ESSAY,
        target_length=1000,
        style="Academic",
    )

    logger.info(f"Testing document writer with topic: {request.topic}")
    result = await writer.execute(request)
    logger.info(f"Result: {result}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(test_document_writer())


def _extract_topic(command: str, doc_type: DocumentType) -> str:
    """Dynamically extract topic from command"""
    command_lower = command.lower()

    # Pattern list (ordered by specificity)
    # Updated to handle -ing forms: write/writing, create/creating, etc.
    patterns = [
        # Pattern 1: "write/writing [me] [an] essay [about/on] topic"
        r'(?:writ(?:e|ing)|creat(?:e|ing)|draft(?:|ing)|compos(?:e|ing)|generat(?:e|ing))\s+(?:me\s+)?(?:an?\s+)?(?:\d+\s+(?:word|page)s?\s+)?(?:essay|report|paper|article|document|blog\s*post)?\s+(?:about|on|regarding)\s+(.+?)(?:\s+in\s+(?:google\s*)?docs?|\s+for\s+me|$)',
        # Pattern 2: "essay [about/on] topic"
        r'(?:essay|report|paper|article|document)\s+(?:about|on|regarding)\s+(.+?)(?:\s+in\s+(?:google\s*)?docs?|$)',
        # Pattern 3: "write/writing [me] [an] topic essay"
        r'(?:writ(?:e|ing)|creat(?:e|ing)|draft(?:|ing))\s+(?:me\s+)?(?:an?\s+)?(.+?)(?:\s+essay|\s+report|\s+paper|\s+article|$)',
        # Pattern 4: Simple "writing essay on topic" (no "about" connector)
        r'(?:writ(?:e|ing)|creat(?:e|ing))\s+essay\s+on\s+(.+?)(?:\s+in\s+(?:google\s*)?docs?|$)',
    ]

    for pattern in patterns:
        match = re.search(pattern, command_lower, re.IGNORECASE)
        if match:
            topic = match.group(1).strip()
            # Clean up the topic
            topic = re.sub(r'\s+in\s+(?:google\s+)?docs?$', '', topic)
            topic = re.sub(r'\s+for\s+me$', '', topic)
            topic = re.sub(r'^\d+\s+(?:word|page)s?\s+', '', topic)
            if topic:
                logger.info(f"Extracted topic '{topic}' from command using pattern: {pattern[:50]}...")
                return topic

    # Fallback: if no pattern matched, log and return default
    logger.warning(f"Could not extract topic from command: '{command}'. Using default.")
    return "the requested topic"