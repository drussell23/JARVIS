from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum
import re
from transformers import pipeline, AutoTokenizer, AutoModelForTokenClassification, AutoModelForSequenceClassification
from datetime import datetime, timedelta
import json

class Intent(Enum):
    """Common conversation intents"""
    GREETING = "greeting"
    FAREWELL = "farewell"
    QUESTION = "question"
    REQUEST_INFO = "request_info"
    REQUEST_ACTION = "request_action"
    CONFIRMATION = "confirmation"
    DENIAL = "denial"
    GRATITUDE = "gratitude"
    APOLOGY = "apology"
    OPINION = "opinion"
    SMALL_TALK = "small_talk"
    HELP = "help"
    FEEDBACK = "feedback"
    TASK_PLANNING = "task_planning"
    CLARIFICATION = "clarification"
    UNKNOWN = "unknown"

@dataclass
class Entity:
    """Represents an extracted entity"""
    text: str
    type: str
    start: int
    end: int
    confidence: float = 1.0
    metadata: Optional[Dict] = None

@dataclass
class IntentResult:
    """Result of intent classification"""
    intent: Intent
    confidence: float
    sub_intents: Optional[List[Tuple[Intent, float]]] = None

@dataclass
class NLPAnalysis:
    """Complete NLP analysis result"""
    text: str
    intent: IntentResult
    entities: List[Entity]
    sentiment: Dict[str, float]
    keywords: List[str]
    language: str
    is_question: bool
    requires_action: bool
    topic: Optional[str] = None

class NLPEngine:
    """Advanced NLP processing engine for chatbot enhancement"""
    
    def __init__(self):
        """Initialize NLP components"""
        # Load spaCy model for entity recognition
        try:
            import spacy
            self.nlp = spacy.load("en_core_web_sm")
        except Exception:
            # Fallback if spacy model not installed
            self.nlp = None
            print("Warning: spaCy model not found. Entity extraction will be limited.")
        
        # Intent patterns (rule-based + ML hybrid approach)
        self.intent_patterns = self._initialize_intent_patterns()
        
        # Load sentiment analysis model
        try:
            self.sentiment_analyzer = pipeline(
                "sentiment-analysis", 
                model="distilbert-base-uncased-finetuned-sst-2-english"
            )
        except:
            self.sentiment_analyzer = None
            print("Warning: Sentiment model not found. Sentiment analysis will be limited.")
        
        # Keywords for different domains
        self.domain_keywords = self._initialize_domain_keywords()
        
    def _initialize_intent_patterns(self) -> Dict[Intent, List[re.Pattern]]:
        """Initialize regex patterns for intent detection"""
        return {
            Intent.GREETING: [
                re.compile(r'\b(hi|hello|hey|greetings|good morning|good afternoon|good evening)\b', re.I),
                re.compile(r'\b(how are you|how do you do|what\'s up|sup)\b', re.I)
            ],
            Intent.FAREWELL: [
                re.compile(r'\b(bye|goodbye|see you|farewell|take care|good night)\b', re.I),
                re.compile(r'\b(talk to you later|ttyl|catch you later)\b', re.I)
            ],
            Intent.QUESTION: [
                re.compile(r'^(what|when|where|who|why|how|which|can|could|would|should|is|are|do|does)\b', re.I),
                re.compile(r'\?$')
            ],
            Intent.REQUEST_ACTION: [
                re.compile(r'\b(please|can you|could you|would you|will you|i need|i want)\b.*\b(do|make|create|help|show|tell|give|send|write|find)\b', re.I),
                re.compile(r'\b(help me|assist me|do something)\b', re.I)
            ],
            Intent.GRATITUDE: [
                re.compile(r'\b(thank|thanks|appreciate|grateful|cheers)\b', re.I),
                re.compile(r'\b(thank you|thanks a lot|much appreciated)\b', re.I)
            ],
            Intent.CONFIRMATION: [
                re.compile(r'\b(yes|yeah|yep|sure|okay|ok|correct|right|exactly|indeed|absolutely|definitely)\b', re.I),
                re.compile(r'\b(that\'s right|you\'re right|i agree)\b', re.I)
            ],
            Intent.DENIAL: [
                re.compile(r'\b(no|nope|not|never|incorrect|wrong|false)\b', re.I),
                re.compile(r'\b(i don\'t|i disagree|that\'s wrong)\b', re.I)
            ],
            Intent.HELP: [
                re.compile(r'\b(help|assist|support|guide|explain)\b', re.I),
                re.compile(r'\b(how to|what is|explain to me)\b', re.I)
            ],
            Intent.TASK_PLANNING: [
                re.compile(r'\b(plan|schedule|organize|steps|process|workflow)\b', re.I),
                re.compile(r'\b(let\'s plan|help me plan|create a plan)\b', re.I)
            ]
        }
    
    def _initialize_domain_keywords(self) -> Dict[str, List[str]]:
        """Initialize domain-specific keywords"""
        return {
            "technology": ["software", "code", "programming", "app", "website", "database", "api", "algorithm"],
            "business": ["meeting", "deadline", "project", "budget", "client", "report", "presentation"],
            "personal": ["feeling", "mood", "day", "weekend", "family", "friend", "hobby"],
            "education": ["learn", "study", "course", "lesson", "teach", "understand", "explain"],
            "task": ["todo", "task", "complete", "finish", "start", "work on", "accomplish"]
        }
    
    def analyze(self, text: str) -> NLPAnalysis:
        """Perform complete NLP analysis on input text"""
        # Basic preprocessing
        text_lower = text.lower().strip()
        
        # Intent recognition
        intent_result = self._recognize_intent(text)
        
        # Entity extraction
        entities = self._extract_entities(text)
        
        # Sentiment analysis
        sentiment = self._analyze_sentiment(text)
        
        # Keyword extraction
        keywords = self._extract_keywords(text)
        
        # Determine if it's a question
        is_question = self._is_question(text)
        
        # Determine if action is required
        requires_action = self._requires_action(text, intent_result.intent)
        
        # Determine topic/domain
        topic = self._determine_topic(text, keywords)
        
        return NLPAnalysis(
            text=text,
            intent=intent_result,
            entities=entities,
            sentiment=sentiment,
            keywords=keywords,
            language="en",  # Could be enhanced with language detection
            is_question=is_question,
            requires_action=requires_action,
            topic=topic
        )
    
    def _recognize_intent(self, text: str) -> IntentResult:
        """Recognize the primary intent of the text"""
        text_lower = text.lower()
        intent_scores = {}
        
        # Check against patterns
        for intent, patterns in self.intent_patterns.items():
            score = 0
            for pattern in patterns:
                if pattern.search(text_lower):
                    score += 1
            if score > 0:
                intent_scores[intent] = score
        
        # Normalize scores
        if intent_scores:
            max_score = max(intent_scores.values())
            intent_scores = {k: v/max_score for k, v in intent_scores.items()}
            
            # Get primary intent
            primary_intent = max(intent_scores, key=intent_scores.get)
            confidence = intent_scores[primary_intent]
            
            # Get sub-intents
            sub_intents = [(k, v) for k, v in intent_scores.items() if k != primary_intent and v > 0.5]
            
            return IntentResult(
                intent=primary_intent,
                confidence=confidence,
                sub_intents=sub_intents if sub_intents else None
            )
        
        return IntentResult(intent=Intent.UNKNOWN, confidence=0.5)
    
    def _extract_entities(self, text: str) -> List[Entity]:
        """Extract named entities from text"""
        entities = []
        
        if self.nlp:
            doc = self.nlp(text)
            for ent in doc.ents:
                entities.append(Entity(
                    text=ent.text,
                    type=ent.label_,
                    start=ent.start_char,
                    end=ent.end_char
                ))
        
        # Additional pattern-based entity extraction
        # Extract dates
        date_pattern = re.compile(r'\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2}|today|tomorrow|yesterday)\b', re.I)
        for match in date_pattern.finditer(text):
            entities.append(Entity(
                text=match.group(),
                type="DATE",
                start=match.start(),
                end=match.end()
            ))
        
        # Extract times
        time_pattern = re.compile(r'\b(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]m)?|\d{1,2}\s*[ap]m)\b', re.I)
        for match in time_pattern.finditer(text):
            entities.append(Entity(
                text=match.group(),
                type="TIME",
                start=match.start(),
                end=match.end()
            ))
        
        # Extract emails
        email_pattern = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')
        for match in email_pattern.finditer(text):
            entities.append(Entity(
                text=match.group(),
                type="EMAIL",
                start=match.start(),
                end=match.end()
            ))
        
        return entities
    
    def _analyze_sentiment(self, text: str) -> Dict[str, float]:
        """Analyze sentiment of the text"""
        if self.sentiment_analyzer:
            try:
                result = self.sentiment_analyzer(text[:512])[0]  # Limit text length
                label = result['label'].lower()
                score = result['score']
                
                if label == 'positive':
                    return {"positive": score, "negative": 1 - score, "neutral": 0}
                else:
                    return {"positive": 1 - score, "negative": score, "neutral": 0}
            except:
                pass
        
        # Fallback to simple rule-based sentiment
        positive_words = ['good', 'great', 'excellent', 'happy', 'love', 'wonderful', 'fantastic']
        negative_words = ['bad', 'terrible', 'hate', 'awful', 'horrible', 'disappointing']
        
        text_lower = text.lower()
        pos_count = sum(1 for word in positive_words if word in text_lower)
        neg_count = sum(1 for word in negative_words if word in text_lower)
        
        total = pos_count + neg_count
        if total == 0:
            return {"positive": 0.0, "negative": 0.0, "neutral": 1.0}
        
        return {
            "positive": pos_count / total,
            "negative": neg_count / total,
            "neutral": 0.0
        }
    
    def _extract_keywords(self, text: str) -> List[str]:
        """Extract important keywords from text"""
        keywords = []
        
        if self.nlp:
            doc = self.nlp(text)
            # Extract noun phrases and important words
            keywords.extend([chunk.text.lower() for chunk in doc.noun_chunks])
            keywords.extend([token.text.lower() for token in doc if token.pos_ in ['NOUN', 'VERB'] and len(token.text) > 3])
        else:
            # Simple keyword extraction
            words = re.findall(r'\b\w{4,}\b', text.lower())
            # Filter out common words
            stop_words = {'that', 'this', 'what', 'when', 'where', 'which', 'would', 'could', 'should'}
            keywords = [w for w in words if w not in stop_words]
        
        return list(set(keywords))[:10]  # Return top 10 unique keywords
    
    def _is_question(self, text: str) -> bool:
        """Determine if the text is a question"""
        question_words = ['what', 'when', 'where', 'who', 'why', 'how', 'which', 'can', 'could', 'would', 'should', 'is', 'are', 'do', 'does']
        text_lower = text.lower().strip()
        
        # Check for question mark
        if text.strip().endswith('?'):
            return True
        
        # Check for question words at the beginning
        first_word = text_lower.split()[0] if text_lower.split() else ""
        return first_word in question_words
    
    def _requires_action(self, text: str, intent: Intent) -> bool:
        """Determine if the text requires an action from the assistant"""
        action_intents = [
            Intent.REQUEST_ACTION,
            Intent.REQUEST_INFO,
            Intent.HELP,
            Intent.TASK_PLANNING,
            Intent.QUESTION
        ]
        
        if intent in action_intents:
            return True
        
        # Check for action words
        action_words = ['please', 'can you', 'could you', 'would you', 'help', 'assist', 'show', 'tell', 'explain', 'create', 'make']
        text_lower = text.lower()
        
        return any(word in text_lower for word in action_words)
    
    def _determine_topic(self, text: str, keywords: List[str]) -> Optional[str]:
        """Determine the main topic/domain of the conversation"""
        text_lower = text.lower()
        topic_scores = {}
        
        for topic, topic_keywords in self.domain_keywords.items():
            score = sum(1 for kw in topic_keywords if kw in text_lower or any(kw in keyword for keyword in keywords))
            if score > 0:
                topic_scores[topic] = score
        
        if topic_scores:
            return max(topic_scores, key=topic_scores.get)
        
        return None

class ConversationFlow:
    """Manages conversation flow and context"""
    
    def __init__(self):
        self.current_topic: Optional[str] = None
        self.pending_clarifications: List[str] = []
        self.task_context: Dict[str, Any] = {}
        self.conversation_state: str = "idle"  # idle, questioning, clarifying, executing
        self.context_memory: List[Dict] = []
        
    def update_flow(self, nlp_result: NLPAnalysis, response: str):
        """Update conversation flow based on NLP analysis"""
        # Update topic
        if nlp_result.topic:
            self.current_topic = nlp_result.topic
        
        # Update conversation state
        if nlp_result.intent.intent == Intent.QUESTION:
            self.conversation_state = "questioning"
        elif nlp_result.intent.intent == Intent.CLARIFICATION:
            self.conversation_state = "clarifying"
        elif nlp_result.intent.intent in [Intent.REQUEST_ACTION, Intent.TASK_PLANNING]:
            self.conversation_state = "executing"
        else:
            self.conversation_state = "idle"
        
        # Store context
        self.context_memory.append({
            "timestamp": datetime.now().isoformat(),
            "intent": nlp_result.intent.intent.value,
            "entities": [{"text": e.text, "type": e.type} for e in nlp_result.entities],
            "topic": nlp_result.topic,
            "sentiment": nlp_result.sentiment
        })
        
        # Keep only recent context
        if len(self.context_memory) > 10:
            self.context_memory = self.context_memory[-10:]
    
    def get_context_summary(self) -> Dict:
        """Get a summary of the current conversation context"""
        return {
            "current_topic": self.current_topic,
            "conversation_state": self.conversation_state,
            "pending_clarifications": self.pending_clarifications,
            "recent_intents": [ctx["intent"] for ctx in self.context_memory[-5:]],
            "task_context": self.task_context
        }

class TaskPlanner:
    """Basic task planning capabilities"""
    
    def __init__(self):
        self.task_templates = self._initialize_task_templates()
    
    def _initialize_task_templates(self) -> Dict[str, List[str]]:
        """Initialize common task templates"""
        return {
            "research": [
                "Define the research question or topic",
                "Gather initial information and sources",
                "Analyze and synthesize findings",
                "Draw conclusions",
                "Present results"
            ],
            "writing": [
                "Outline the main points",
                "Write the introduction",
                "Develop the body content",
                "Write the conclusion",
                "Review and edit"
            ],
            "problem_solving": [
                "Identify and define the problem",
                "Gather relevant information",
                "Generate possible solutions",
                "Evaluate solutions",
                "Implement the best solution",
                "Review results"
            ],
            "learning": [
                "Set learning objectives",
                "Identify resources",
                "Create a study schedule",
                "Practice and apply concepts",
                "Test understanding",
                "Review and reinforce"
            ]
        }
    
    def create_task_plan(self, task_description: str, nlp_result: NLPAnalysis) -> Dict[str, Any]:
        """Create a basic task plan based on the description"""
        # Determine task type
        task_type = self._determine_task_type(task_description, nlp_result.keywords)
        
        # Get base template
        if task_type in self.task_templates:
            steps = self.task_templates[task_type]
        else:
            # Generic task template
            steps = [
                "Understand the requirements",
                "Plan the approach",
                "Execute the main task",
                "Verify the results",
                "Complete and deliver"
            ]
        
        # Extract time entities
        time_entities = [e for e in nlp_result.entities if e.type in ["DATE", "TIME"]]
        deadline = time_entities[0].text if time_entities else None
        
        return {
            "task": task_description,
            "type": task_type,
            "steps": steps,
            "deadline": deadline,
            "entities": [{"text": e.text, "type": e.type} for e in nlp_result.entities],
            "keywords": nlp_result.keywords[:5],
            "created_at": datetime.now().isoformat()
        }
    
    def _determine_task_type(self, description: str, keywords: List[str]) -> str:
        """Determine the type of task"""
        desc_lower = description.lower()
        
        task_indicators = {
            "research": ["research", "investigate", "study", "analyze", "explore"],
            "writing": ["write", "draft", "compose", "document", "report"],
            "problem_solving": ["solve", "fix", "resolve", "troubleshoot", "debug"],
            "learning": ["learn", "understand", "study", "practice", "master"]
        }
        
        for task_type, indicators in task_indicators.items():
            if any(ind in desc_lower or any(ind in kw for kw in keywords) for ind in indicators):
                return task_type
        
        return "general"

class ResponseQualityEnhancer:
    """Improves response quality through various techniques"""
    
    def __init__(self):
        self.quality_rules = self._initialize_quality_rules()
    
    def _initialize_quality_rules(self) -> Dict[str, Any]:
        """Initialize response quality rules"""
        return {
            "clarity": {
                "avoid_words": ["thing", "stuff", "whatever", "like", "you know"],
                "prefer_specific": True,
                "max_sentence_length": 25
            },
            "engagement": {
                "use_active_voice": True,
                "vary_sentence_structure": True,
                "include_examples": True
            },
            "completeness": {
                "answer_all_questions": True,
                "provide_context": True,
                "include_next_steps": True
            }
        }
    
    def enhance_response(self, response: str, nlp_result: NLPAnalysis, context: Dict) -> str:
        """Enhance the quality of a response"""
        enhanced = response
        
        # Apply enhancements based on intent
        if nlp_result.intent.intent == Intent.QUESTION:
            enhanced = self._enhance_answer(enhanced, nlp_result)
        elif nlp_result.intent.intent == Intent.REQUEST_ACTION:
            enhanced = self._enhance_action_response(enhanced, nlp_result)
        elif nlp_result.intent.intent == Intent.HELP:
            enhanced = self._enhance_help_response(enhanced, nlp_result)
        
        # General enhancements
        enhanced = self._improve_clarity(enhanced)
        enhanced = self._ensure_completeness(enhanced, nlp_result)
        
        return enhanced
    
    def _enhance_answer(self, response: str, nlp_result: NLPAnalysis) -> str:
        """Enhance answers to questions"""
        # Ensure the answer directly addresses the question
        if nlp_result.is_question and not any(word in response.lower() for word in ["yes", "no", "because", "the answer"]):
            response = f"To answer your question: {response}"
        
        return response
    
    def _enhance_action_response(self, response: str, nlp_result: NLPAnalysis) -> str:
        """Enhance responses to action requests"""
        # Add confirmation of understanding
        if not any(word in response.lower() for word in ["will", "can", "sure", "happy to"]):
            response = f"I'll help you with that. {response}"
        
        return response
    
    def _enhance_help_response(self, response: str, nlp_result: NLPAnalysis) -> str:
        """Enhance help responses"""
        # Structure help responses better
        if ":" not in response:
            response = f"Here's how I can help: {response}"
        
        return response
    
    def _improve_clarity(self, response: str) -> str:
        """Improve response clarity"""
        # Replace vague words
        vague_replacements = {
            "thing": "item",
            "stuff": "content",
            "whatever": "anything relevant",
            "like": "",
            "you know": ""
        }
        
        for vague, replacement in vague_replacements.items():
            response = response.replace(f" {vague} ", f" {replacement} " if replacement else " ")
        
        # Clean up extra spaces
        response = " ".join(response.split())
        
        return response
    
    def _ensure_completeness(self, response: str, nlp_result: NLPAnalysis) -> str:
        """Ensure response completeness"""
        # Add next steps if action was requested
        if nlp_result.requires_action and not any(word in response.lower() for word in ["next", "then", "after"]):
            response += " Let me know if you need any clarification or have questions."
        
        return response