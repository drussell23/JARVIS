"""
Simple Chatbot for M1 Macs - No heavy models required
Async version for better performance
"""

import logging
from typing import List, Dict, Optional, AsyncGenerator
from datetime import datetime
from dataclasses import dataclass, field
import random
import asyncio

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class ConversationTurn:
    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime = field(default_factory=datetime.now)

class SimpleChatbot:
    """
    A simple chatbot that works immediately without heavy models.
    Perfect for M1 Macs while we set up a proper model.
    """

    def __init__(
        self,
        max_history_length: int = 10,
        model_name: str = "simple-chat",
    ):
        self.max_history_length = max_history_length
        self.model_name = model_name
        self.conversation_history: List[ConversationTurn] = []
        self._lock = asyncio.Lock()  # For thread-safe history access

        # System personality
        self.personality = "helpful and friendly AI assistant named JARVIS"

        # Pre-defined responses for common queries
        self.responses = {
            "greeting": [
                "Hello! I'm JARVIS, your AI assistant. How can I help you today?",
                "Hi there! I'm here to assist you. What can I do for you?",
                "Greetings! I'm JARVIS. How may I be of service?",
            ],
            "how_are_you": [
                "I'm functioning perfectly, thank you for asking! How can I help you?",
                "All systems operational! What can I assist you with today?",
                "I'm doing great! Ready to help with whatever you need.",
            ],
            "capabilities": [
                "I can help with conversations, answer questions, and assist with various tasks. What would you like to know?",
                "I'm designed to be a helpful assistant. I can chat, provide information, and help brainstorm ideas. What interests you?",
                "I can engage in conversations, help with questions, and provide assistance on many topics. What shall we discuss?",
            ],
            "thanks": [
                "You're welcome! Is there anything else I can help with?",
                "My pleasure! Feel free to ask if you need anything else.",
                "Happy to help! Let me know if there's more I can do for you.",
            ],
            "goodbye": [
                "Goodbye! Feel free to come back anytime you need assistance.",
                "Take care! I'll be here whenever you need help.",
                "See you later! Don't hesitate to return if you need anything.",
            ],
            "default": [
                "That's an interesting point. Could you tell me more?",
                "I understand. How can I assist you with that?",
                "I see. What would you like to know about this topic?",
                "That's a good question. Let me help you with that.",
                "I'm here to help. Could you provide more details?",
            ],
        }

        # Initialize components if available
        try:
            from engines.nlp_engine import (
                NLPEngine,
                ConversationFlow,
                TaskPlanner,
                ResponseQualityEnhancer,
            )

            self.nlp_engine = NLPEngine()
            self.conversation_flow = ConversationFlow()
            self.task_planner = TaskPlanner()
            self.response_enhancer = ResponseQualityEnhancer()
        except Exception as e:
            logger.warning(f"NLP components not available: {e}")
            self.nlp_engine = None
            self.conversation_flow = None
            self.task_planner = None
            self.response_enhancer = None

    async def add_to_history(self, role: str, content: str):
        """Add a conversation turn to history."""
        async with self._lock:
            turn = ConversationTurn(role=role, content=content)
            self.conversation_history.append(turn)

            # Maintain history length limit
            if len(self.conversation_history) > self.max_history_length * 2:
                self.conversation_history = self.conversation_history[
                    -self.max_history_length * 2 :
                ]

    async def get_conversation_history(self) -> List[Dict]:
        """Get the conversation history as a list of dictionaries."""
        async with self._lock:
            return [
                {
                    "role": turn.role,
                    "content": turn.content,
                    "timestamp": turn.timestamp.isoformat(),
                }
                for turn in self.conversation_history
            ]

    async def clear_history(self):
        """Clear the conversation history."""
        async with self._lock:
            self.conversation_history = []

    async def generate_response(self, user_input: str) -> str:
        """
        Generate a response based on pattern matching and context.
        """
        # Add user input to history
        await self.add_to_history("user", user_input)

        user_lower = user_input.lower()

        # Determine response category
        if any(
            word in user_lower
            for word in [
                "hello",
                "hi",
                "hey",
                "greetings",
                "good morning",
                "good afternoon",
                "good evening",
            ]
        ):
            response = random.choice(self.responses["greeting"])
        elif any(
            phrase in user_lower
            for phrase in [
                "how are you",
                "how do you do",
                "how's it going",
                "how are things",
            ]
        ):
            response = random.choice(self.responses["how_are_you"])
        elif any(
            phrase in user_lower
            for phrase in [
                "what can you do",
                "your capabilities",
                "what are you capable of",
                "help me",
            ]
        ):
            response = random.choice(self.responses["capabilities"])
        elif any(word in user_lower for word in ["thank", "thanks", "appreciate"]):
            response = random.choice(self.responses["thanks"])
        elif any(
            word in user_lower
            for word in ["bye", "goodbye", "farewell", "see you", "take care"]
        ):
            response = random.choice(self.responses["goodbye"])
        elif "?" in user_input:
            # It's a question
            if "what" in user_lower:
                # Check if it's a math question
                if any(op in user_input for op in ['+', '-', '*', '/', '^', '**', 'plus', 'minus', 'times', 'divided', 'add', 'subtract', 'multiply', 'power', 'squared', 'cubed']) or any(char in user_input for char in '+-*/^'):
                    try:
                        # Try to use quantized LLM for math
                        from chatbots.quantized_llm_wrapper import get_quantized_llm
                        llm = get_quantized_llm()
                        if llm.initialized or llm.initialize():
                            math_response = llm.generate(user_input, max_tokens=50, temperature=0.1)
                            response = math_response.strip()
                        else:
                            response = "I'd love to help with that calculation, but I need the language model to be loaded first. Try running: python setup_m1_optimized_llm.py"
                    except Exception:
                        response = "I can help with math, but the calculation engine isn't available right now."
                else:
                    response = "That's a great question! Based on what you're asking, I'd be happy to help explain or provide information."
            elif "how" in user_lower:
                response = "Let me help you understand how that works. Could you be more specific about what aspect you'd like to know?"
            elif "why" in user_lower:
                response = "That's a thoughtful question about the reasoning. Let me share my perspective on that."
            elif "when" in user_lower:
                response = "Regarding the timing, that depends on several factors. Could you provide more context?"
            elif "where" in user_lower:
                response = "For location-related questions, I'd need more specific details to give you accurate information."
            else:
                response = random.choice(self.responses["default"])
        else:
            # General statement
            response = random.choice(self.responses["default"])

        # Add some context awareness
        async with self._lock:
            if len(self.conversation_history) > 2:
                last_user_msg = (
                    self.conversation_history[-2].content.lower()
                    if len(self.conversation_history) > 1
                    else ""
                )
                if "?" in last_user_msg and "?" not in user_input:
                    response = f"I see. {response}"

        # Add to history
        await self.add_to_history("assistant", response)

        return response

    async def generate_response_with_context(
        self, user_input: str, context: Optional[Dict] = None
    ) -> Dict:
        """
        Generate a response with metadata (compatible with existing API).
        """
        start_time = datetime.now()

        # Perform NLP analysis if available
        nlp_result = None
        if self.nlp_engine:
            try:
                # Run NLP analysis in thread pool to avoid blocking
                nlp_result = await asyncio.get_event_loop().run_in_executor(
                    None, self.nlp_engine.analyze, user_input
                )
            except Exception as e:
                logger.warning(f"NLP analysis failed: {e}")

        # Generate response
        response = await self.generate_response(user_input)

        # Calculate generation time
        generation_time = (datetime.now() - start_time).total_seconds()

        return {
            "response": response,
            "generation_time": generation_time,
            "model_used": self.model_name,
            "conversation_id": id(self),
            "turn_number": len(self.conversation_history) // 2,
            "context": context,
            "nlp_analysis": {
                "intent": nlp_result.intent.intent.value if nlp_result else "unknown",
                "intent_confidence": (
                    nlp_result.intent.confidence if nlp_result else 0.0
                ),
                "entities": [
                    {"text": e.text, "type": e.type}
                    for e in (nlp_result.entities if nlp_result else [])
                ],
                "sentiment": (
                    nlp_result.sentiment
                    if nlp_result
                    else {"positive": 0, "negative": 0, "neutral": 1}
                ),
                "is_question": nlp_result.is_question if nlp_result else False,
                "requires_action": nlp_result.requires_action if nlp_result else False,
                "topic": nlp_result.topic if nlp_result else None,
                "keywords": nlp_result.keywords[:5] if nlp_result else [],
            },
            "conversation_flow": (
                self.conversation_flow.get_context_summary()
                if self.conversation_flow
                else {}
            ),
            "task_plan": None,
        }

    def set_system_prompt(self, prompt: str):
        """Update the system prompt."""
        self.personality = prompt

    # Compatibility methods
    def _get_generation_config(self) -> Dict:
        """Get default generation config (for compatibility)"""
        return {}

    @property
    def model(self):
        """Compatibility property"""
        return type(
            "obj",
            (object,),
            {"config": type("obj", (object,), {"name_or_path": self.model_name})},
        )

    @property
    def tokenizer(self):
        """Compatibility property"""
        return type("obj", (object,), {"pad_token_id": 0, "eos_token_id": 2})

    # Make it compatible with M1Chatbot methods
    async def generate_response_stream(
        self, user_input: str
    ) -> AsyncGenerator[str, None]:
        """Streaming response generator for compatibility"""
        response = await self.generate_response(user_input)
        words = response.split()
        for word in words:
            yield word + " "
            await asyncio.sleep(0.05)  # Small delay to simulate streaming

    async def cleanup(self):
        """Cleanup method for compatibility"""
        logger.info("SimpleChatbot cleanup (no-op)")
        # SimpleChatbot doesn't have resources to clean up
        pass

    def get_capabilities(self) -> Dict[str, any]:
        """Get chatbot capabilities"""
        return {
            "model_type": "simple",
            "model_name": self.model_name,
            "has_nlp": self.nlp_engine is not None,
            "has_math": hasattr(self, 'math_detector') and self.math_detector is not None,
            "streaming": True,
            "context_aware": True,
            "max_history": self.max_history_length,
            "personality": self.personality
        }

    async def add_knowledge(self, content: str, metadata: Optional[Dict] = None) -> Dict:
        """Add knowledge (not supported in simple mode)"""
        return {
            "success": False,
            "error": "Knowledge management not available in Simple mode",
            "suggestion": "Upgrade to Intelligent or LangChain mode for knowledge management"
        }

# Test the chatbot
if __name__ == "__main__":

    async def test_chatbot():
        print("Testing Simple Chatbot...")
        bot = SimpleChatbot()

        test_inputs = [
            "Hello!",
            "How are you today?",
            "What can you do?",
            "Can you help me with Python?",
            "Thanks!",
            "Goodbye!",
        ]

        for user_input in test_inputs:
            print(f"\nUser: {user_input}")
            response_data = await bot.generate_response_with_context(user_input)
            print(f"Assistant: {response_data['response']}")
            print(f"(Generated in {response_data['generation_time']:.4f} seconds)")

    # Run the async test
    asyncio.run(test_chatbot())
