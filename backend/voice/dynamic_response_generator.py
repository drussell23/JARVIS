"""
Dynamic Response Generator for JARVIS
Creates natural, varied, contextual responses without hardcoding
"""

import random
from typing import List, Optional, Dict, Any
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class DynamicResponseGenerator:
    """Generate natural, varied JARVIS responses based on context"""
    
    def __init__(self, user_name: str = "User"):
        self.user_name = user_name
        self.conversation_history = []
        self.sir_usage_count = 0
        self.total_responses = 0
        self.last_sir_used = 0
        self.last_phrases = []  # Track recent phrases to avoid repetition
        self.max_phrase_history = 10  # Remember last 10 phrases
        
        # Personality traits that affect response style
        self.personality = {
            'formality': 0.7,  # 0-1 scale, higher = more formal
            'wit': 0.8,        # Tendency to use clever phrases
            'verbosity': 0.5,  # How detailed responses are
            'warmth': 0.6,     # Emotional warmth in responses
        }
        
    def should_use_sir(self) -> bool:
        """Intelligently decide whether to use 'Sir' based on context"""
        # Use Sir roughly 20-30% of the time, but with smart spacing
        self.total_responses += 1
        
        # Never use Sir twice in a row
        if self.last_sir_used == self.total_responses - 1:
            return False
            
        # Calculate Sir usage rate
        sir_rate = self.sir_usage_count / max(self.total_responses, 1)
        
        # If we've been using it too much, skip it
        if sir_rate > 0.3:
            return False
            
        # Random chance with context
        chance = random.random()
        use_sir = chance < 0.25  # Base 25% chance
        
        # Increase chance for important moments
        if self.total_responses == 1:  # First interaction
            use_sir = chance < 0.5
        elif self.total_responses % 10 == 0:  # Every 10th response
            use_sir = chance < 0.4
            
        if use_sir:
            self.sir_usage_count += 1
            self.last_sir_used = self.total_responses
            
        return use_sir
    
    def get_acknowledgment(self, context: Dict[str, Any] = None) -> str:
        """Generate varied acknowledgment phrases"""
        formal_acks = [
            "Certainly", "Of course", "Right away", "Understood", 
            "Very well", "As you wish", "Indeed", "Affirmative",
            "Acknowledged", "By all means"
        ]
        
        casual_acks = [
            "Got it", "Sure thing", "On it", "No problem",
            "Absolutely", "You bet", "Will do", "Alright",
            "Sounds good", "Perfect", "Great", "Okay"
        ]
        
        creative_acks = [
            "Consider it done", "Leave it to me", "I'm on the case",
            "Coming right up", "Happy to help", "Let me handle that",
            "I'll take care of it", "Allow me", "Right on it",
            "Let's do this", "I've got this"
        ]
        
        # Mix based on personality
        if self.personality['formality'] > 0.6:
            pool = formal_acks + random.sample(casual_acks, 2)
        else:
            pool = casual_acks + random.sample(formal_acks, 2)
            
        if self.personality['wit'] > 0.7:
            pool.extend(random.sample(creative_acks, 3))
            
        # Filter out recently used phrases
        available_pool = [p for p in pool if p not in self.last_phrases]
        if not available_pool:  # If all phrases were recently used, reset
            available_pool = pool
            self.last_phrases = []
            
        ack = random.choice(available_pool)
        
        # Track this phrase
        self.last_phrases.append(ack)
        if len(self.last_phrases) > self.max_phrase_history:
            self.last_phrases.pop(0)
        
        # Occasionally add Sir
        if self.should_use_sir():
            if random.random() < 0.5:
                ack = f"{ack}, {self.user_name or 'Sir'}"
            else:
                ack = f"{ack}, Sir"
                
        return ack
    
    def get_transition_phrase(self) -> str:
        """Generate natural transition phrases"""
        transitions = [
            "Moving on to", "Now handling", "Shifting to", "Let's proceed with",
            "Next up", "Turning to", "Working on", "Processing",
            "Let me", "I'll now", "Time to", "Beginning"
        ]
        return random.choice(transitions)
    
    def get_progress_update(self, progress: int, context: Dict[str, Any] = None) -> str:
        """Generate varied progress updates"""
        if progress < 25:
            stage_phrases = [
                "Just getting started", "In the early stages", "Beginning phase",
                "Initial progress", "Starting out", "Early on"
            ]
        elif progress < 50:
            stage_phrases = [
                "Making progress", "Moving along nicely", "Gaining momentum",
                "Well underway", "Progressing steadily", "Building up"
            ]
        elif progress < 75:
            stage_phrases = [
                "Over halfway", "Making good headway", "Well along",
                "Substantial progress", "Moving along well", "Good progress"
            ]
        elif progress < 90:
            stage_phrases = [
                "Nearly there", "Almost done", "In the home stretch",
                "Final stages", "Approaching completion", "Wrapping up"
            ]
        else:
            stage_phrases = [
                "Just about finished", "Final touches", "Nearly complete",
                "Almost ready", "Finishing up", "Last bits"
            ]
            
        phrase = random.choice(stage_phrases)
        
        # Add percentage occasionally
        if random.random() < 0.4:
            phrase = f"{phrase} - {progress}% complete"
            
        # Add Sir occasionally
        if self.should_use_sir():
            phrase = f"{phrase}, Sir"
            
        return phrase
    
    def get_completion_message(self, task_type: str, details: Dict[str, Any] = None) -> str:
        """Generate varied completion messages"""
        success_phrases = [
            "All done", "Complete", "Finished", "Ready", "Done",
            "Task complete", "All set", "Completed successfully"
        ]
        
        base = random.choice(success_phrases)
        
        # Add context-specific details
        if details:
            if 'word_count' in details:
                additions = [
                    f" - {details['word_count']} words written",
                    f". That's {details['word_count']} words",
                    f", {details['word_count']} words total"
                ]
                base += random.choice(additions)
            
            if 'document_type' in details:
                doc_type = details['document_type']
                if random.random() < 0.5:
                    base = f"Your {doc_type} is {base.lower()}"
                    
        # Add Sir occasionally
        if self.should_use_sir():
            base = f"{base}, Sir"
            
        return base
    
    def get_error_message(self, error_type: str = "general", details: str = None) -> str:
        """Generate varied error messages"""
        apologies = [
            "I encountered an issue",
            "There seems to be a problem", 
            "I'm having trouble",
            "Something went wrong",
            "I ran into a snag",
            "There's been a hiccup"
        ]
        
        message = random.choice(apologies)
        
        if details:
            message = f"{message}: {details}"
        else:
            message = f"{message} with that request"
            
        # Be more apologetic occasionally
        if random.random() < 0.3:
            message = f"I apologize, {message}"
            
        # Add Sir less frequently in error messages
        if self.should_use_sir() and random.random() < 0.5:
            message = f"{message}, Sir"
            
        return message
    
    def get_thinking_phrase(self) -> str:
        """Generate phrases for when JARVIS is processing"""
        thinking = [
            "Let me check that",
            "Looking into it",
            "One moment",
            "Processing",
            "Analyzing",
            "Let me see",
            "Checking",
            "Working on it",
            "Give me a second",
            "Examining that",
            "Investigating",
            "Let me find out",
            "Calculating",
            "Searching",
            "Just a moment",
            "Hold on",
            "I'll check"
        ]
        
        # Filter out recently used phrases
        available = [p for p in thinking if p not in self.last_phrases]
        if not available:
            available = thinking
            
        phrase = random.choice(available)
        
        # Track this phrase
        self.last_phrases.append(phrase)
        if len(self.last_phrases) > self.max_phrase_history:
            self.last_phrases.pop(0)
        
        # Add variation less frequently
        if random.random() < 0.2:  # Reduced from 0.3
            additions = ["for you", "now", "right away"]
            phrase = f"{phrase} {random.choice(additions)}"
            
        # Rarely add Sir to thinking phrases
        if self.should_use_sir() and random.random() < 0.15:  # Reduced from 0.2
            phrase = f"{phrase}, Sir"
            
        return phrase
    
    def get_contextual_response(self, 
                               response_type: str,
                               context: Dict[str, Any] = None) -> str:
        """Generate contextual responses based on type and context"""
        
        # Time-based greetings
        hour = datetime.now().hour
        time_context = ""
        if 5 <= hour < 12:
            time_context = "morning"
        elif 12 <= hour < 17:
            time_context = "afternoon"
        elif 17 <= hour < 22:
            time_context = "evening"
        else:
            time_context = "night"
            
        if response_type == "greeting":
            greetings = {
                "morning": ["Good morning", "Morning", "Top of the morning"],
                "afternoon": ["Good afternoon", "Afternoon"],
                "evening": ["Good evening", "Evening"],
                "night": ["Good evening", "Hello"]
            }
            
            greeting = random.choice(greetings.get(time_context, ["Hello"]))
            
            if self.should_use_sir():
                greeting = f"{greeting}, Sir"
            
            # Add status occasionally
            if random.random() < 0.3:
                statuses = [
                    "How can I help?",
                    "What can I do for you?",
                    "At your service",
                    "Ready to assist"
                ]
                greeting = f"{greeting}. {random.choice(statuses)}"
                
            return greeting
            
        elif response_type == "farewell":
            farewells = [
                "Goodbye", "Take care", "Until next time",
                "Standing by", "I'll be here", "Call when you need me"
            ]
            
            farewell = random.choice(farewells)
            
            if self.should_use_sir():
                farewell = f"{farewell}, Sir"
                
            return farewell
            
        elif response_type == "confirmation":
            confirmations = [
                "Confirmed", "Roger that", "Affirmative",
                "Will do", "Consider it done", "On it"
            ]
            
            return random.choice(confirmations)
            
        else:
            # Default contextual response
            return self.get_acknowledgment(context)
    
    def personalize_response(self, base_response: str, 
                           emotion: str = None,
                           urgency: str = None) -> str:
        """Add personality and emotion to responses"""
        
        # Adjust based on emotion
        if emotion == "excited":
            if random.random() < 0.3:
                additions = ["This is interesting!", "Fascinating!", "Excellent choice!"]
                base_response = f"{base_response} {random.choice(additions)}"
                
        elif emotion == "concerned":
            if random.random() < 0.3:
                prefixes = ["I should mention", "Just so you know", "Heads up"]
                base_response = f"{random.choice(prefixes)}, {base_response.lower()}"
                
        # Adjust for urgency
        if urgency == "high":
            base_response = f"Right away! {base_response}"
        elif urgency == "low":
            if random.random() < 0.3:
                base_response = f"When you're ready, {base_response.lower()}"
                
        return base_response
    
    def generate_startup_greeting(self, context: Dict[str, Any] = None) -> str:
        """Generate sophisticated JARVIS-style startup greetings with variety"""
        import platform
        import psutil
        from datetime import datetime
        import random
        
        # Get system context
        hour = datetime.now().hour
        day_name = datetime.now().strftime('%A')
        
        # Determine time of day
        if 5 <= hour < 12:
            time_context = "morning"
        elif 12 <= hour < 17:
            time_context = "afternoon"
        elif 17 <= hour < 22:
            time_context = "evening"
        else:
            time_context = "night"
        
        # Get system status (if available)
        try:
            cpu_percent = psutil.cpu_percent(interval=0.1)
            memory_percent = psutil.virtual_memory().percent
            system_status = "optimal" if cpu_percent < 50 and memory_percent < 70 else "elevated"
        except Exception:
            system_status = "normal"
        
        # JARVIS-style sophisticated greetings
        greetings = {
            "morning": [
                f"Good morning, {self.user_name or 'Sir'}. All systems are operational and ready for your command.",
                f"Welcome back, {self.user_name or 'Sir'}. JARVIS systems initialized. How may I assist you this {day_name} morning?",
                f"Good morning. Systems check complete. Standing by for your instructions.",
                f"Morning, {self.user_name or 'Sir'}. I've completed the startup diagnostics. Everything's running smoothly.",
                f"Systems online, {self.user_name or 'Sir'}. Another beautiful {day_name} morning to be of service.",
                f"Good morning. Neural networks calibrated, voice recognition optimized. At your service.",
                f"Rise and shine, {self.user_name or 'Sir'}. JARVIS fully operational. What's on the agenda today?",
                f"Morning protocols complete. Ready to tackle whatever challenges {day_name} brings.",
            ],
            "afternoon": [
                f"Good afternoon, {self.user_name or 'Sir'}. Systems are at your disposal.",
                f"Welcome back. JARVIS online and ready to assist with your afternoon tasks.",
                f"Afternoon, {self.user_name or 'Sir'}. All systems functioning at peak efficiency.",
                f"System reactivation complete. How may I help you this {day_name} afternoon?",
                f"Good afternoon. Standing by for your commands.",
                f"JARVIS systems restored, {self.user_name or 'Sir'}. Ready to continue where we left off.",
                f"Afternoon. Diagnostics complete, all systems green. What can I do for you?",
            ],
            "evening": [
                f"Good evening, {self.user_name or 'Sir'}. JARVIS at your service.",
                f"Welcome back. Systems online for your evening session.",
                f"Evening, {self.user_name or 'Sir'}. All systems are operational.",
                f"Good evening. Ready to assist with whatever you need this {day_name} evening.",
                f"System activation complete. How may I be of service tonight?",
                f"Evening protocols initiated. Standing by for your instructions.",
                f"JARVIS online, {self.user_name or 'Sir'}. I trust you've had a productive day?",
            ],
            "night": [
                f"Good evening, {self.user_name or 'Sir'}. Working late again, I see.",
                f"Welcome back. JARVIS systems online despite the late hour.",
                f"System activation complete. Ready for your late-night commands.",
                f"JARVIS online. Shall I adjust the display brightness for the evening?",
                f"Late night session initiated. How may I assist you?",
                f"Systems operational, {self.user_name or 'Sir'}. Burning the midnight oil?",
            ]
        }
        
        # Advanced status-aware greetings - build them dynamically
        status_greetings = []
        
        # Build varied status messages
        system_type = random.choice(['primary', 'core', 'essential'])
        op_status = random.choice(['operational', 'online', 'functioning optimally'])
        status_greetings.append(f"JARVIS initialization complete. All {system_type} systems are {op_status}.")
        
        ready_msg = random.choice(['Ready to serve', 'At your command', 'Standing by'])
        status_greetings.append(f"System boot sequence finished. {ready_msg}.")
        
        query = random.choice(['How may I be of assistance?', 'What can I do for you today?', 'Ready for your instructions.'])
        status_greetings.append(f"Welcome back, {self.user_name or 'Sir'}. {query}")
        
        perf_status = random.choice(['All systems nominal', 'Operating at peak performance', 'Systems fully operational'])
        status_greetings.append(f"JARVIS online. {perf_status}.")
        
        ai_status = random.choice(['AI neural pathways synchronized', 'Voice recognition calibrated', 'Machine learning models loaded'])
        status_greetings.append(f"Greetings, {self.user_name or 'Sir'}. {ai_status}. Ready to proceed.")
        
        # Mix time-based and status-based greetings
        greeting_pool = greetings.get(time_context, greetings['morning'])
        
        # Add some status greetings for variety
        if random.random() < 0.3:  # 30% chance to use a status greeting
            greeting_pool.extend(status_greetings)
        
        # Special greetings for specific conditions
        if system_status == "elevated" and random.random() < 0.2:
            special_greetings = [
                f"Systems online, though I'm detecting elevated resource usage. Shall I optimize?",
                f"JARVIS operational. Note: System resources are running a bit high.",
            ]
            greeting_pool.extend(special_greetings)
        
        # Weekend special
        if day_name in ['Saturday', 'Sunday'] and random.random() < 0.3:
            weekend_greetings = [
                f"Happy {day_name}, {self.user_name or 'Sir'}. JARVIS ready for your weekend commands.",
                f"Weekend systems activated. How may I assist you this {day_name}?",
            ]
            greeting_pool.extend(weekend_greetings)
        
        # Filter out recently used greetings if tracking
        if hasattr(self, 'last_startup_greetings'):
            available_greetings = [g for g in greeting_pool if g not in self.last_startup_greetings]
            if not available_greetings:
                available_greetings = greeting_pool
                self.last_startup_greetings = []
        else:
            available_greetings = greeting_pool
            self.last_startup_greetings = []
        
        chosen_greeting = random.choice(available_greetings)
        
        # Track this greeting
        self.last_startup_greetings.append(chosen_greeting)
        if len(self.last_startup_greetings) > 5:  # Remember last 5 startup greetings
            self.last_startup_greetings.pop(0)
        
        return chosen_greeting
    
    def generate_document_narration(self, phase: str, context: Dict[str, Any]) -> str:
        """Generate natural document writing narration"""
        topic = context.get('topic', 'the topic')
        doc_type = context.get('document_type', 'document')
        progress = context.get('progress', 0)
        word_count = context.get('word_count', 0)
        section = context.get('current_section', '')
        
        # Map phases to natural responses
        narrations = {
            'acknowledging_request': [
                f"I'll write that {doc_type} on {topic}",
                f"Starting your {doc_type} about {topic}",
                f"{topic} - {random.choice(['interesting', 'good', 'great', 'excellent', 'fascinating'])} topic",
                f"Let me create that {doc_type} for you",
                f"Beginning work on your {doc_type}",
                f"{doc_type} on {topic} coming up",
                f"I'll prepare that {doc_type}"
            ],
            'analyzing_topic': [
                f"Analyzing {topic}",
                f"Researching key points about {topic}",
                f"Gathering information on {topic}",
                f"Looking into {topic}"
            ],
            'outline_complete': [
                f"Outline ready with {random.choice(['several', 'multiple'])} key points",
                "Structure mapped out",
                "Got the framework",
                "Blueprint complete"
            ],
            'writing_section': [
                f"Writing {section}",
                f"Working on {section}",
                f"Developing {section}",
                f"{self.get_transition_phrase()} {section}",
                f"Crafting {section}",
                f"Composing {section}",
                f"Now on {section}"
            ],
            'progress_update': [
                self.get_progress_update(progress, context),
                f"{word_count} words so far",
                f"Progress: {progress}%"
            ],
            'writing_complete': [
                self.get_completion_message(doc_type, context),
                f"Finished - {word_count} words",
                f"Your {doc_type} is complete"
            ]
        }
        
        # Get appropriate narration
        options = narrations.get(phase, [f"Working on your {doc_type}"])
        narration = random.choice(options)
        
        # Occasionally add Sir (but less frequently)
        if self.should_use_sir():
            narration = f"{narration}, Sir"
            
        return narration


# Global instance for easy access
_response_generator = None

def get_response_generator(user_name: str = "User") -> DynamicResponseGenerator:
    """Get or create the global response generator"""
    global _response_generator
    if _response_generator is None:
        _response_generator = DynamicResponseGenerator(user_name)
    return _response_generator