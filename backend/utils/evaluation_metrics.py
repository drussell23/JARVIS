import torch
import torch.nn as nn
from torch.nn import functional as F
import numpy as np
from typing import Dict, List, Optional, Tuple, Any, Union
from collections import defaultdict, Counter
from dataclasses import dataclass, field
import json
from sklearn.metrics import accuracy_score, f1_score, precision_recall_fscore_support
from nltk.translate.bleu_score import sentence_bleu, SmoothingFunction
from rouge import Rouge
import nltk
from transformers import AutoTokenizer
import re
import math
from tqdm import tqdm

@dataclass
class EvaluationResult:
    """Container for evaluation results"""
    # Basic metrics
    perplexity: float
    loss: float
    
    # Generation quality metrics
    bleu_score: float
    rouge_scores: Dict[str, float]
    meteor_score: float
    
    # Diversity metrics
    distinct_1: float
    distinct_2: float
    self_bleu: float
    
    # Coherence metrics
    coherence_score: float
    consistency_score: float
    
    # Domain-specific metrics
    intent_accuracy: Optional[float] = None
    entity_f1: Optional[float] = None
    domain_accuracy: Optional[float] = None
    
    # Human-like metrics
    fluency_score: Optional[float] = None
    relevance_score: Optional[float] = None
    engagement_score: Optional[float] = None
    
    # Additional metadata
    num_samples: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

class ModelEvaluator:
    """Comprehensive evaluation suite for chatbot models"""
    
    def __init__(
        self,
        tokenizer: AutoTokenizer,
        device: str = "cuda" if torch.cuda.is_available() else "cpu"
    ):
        self.tokenizer = tokenizer
        self.device = device
        self.rouge = Rouge()
        
        # Download required NLTK data
        try:
            nltk.download('punkt', quiet=True)
            nltk.download('wordnet', quiet=True)
            nltk.download('stopwords', quiet=True)
        except Exception:
            pass
            
    def evaluate_model(
        self,
        model: nn.Module,
        eval_dataloader,
        compute_generation_metrics: bool = True,
        max_generation_length: int = 100
    ) -> EvaluationResult:
        """Comprehensive model evaluation"""
        model.eval()
        
        # Initialize metric collectors
        total_loss = 0
        all_predictions = []
        all_references = []
        all_generated_texts = []
        intent_predictions = []
        intent_labels = []
        domain_predictions = []
        domain_labels = []
        
        with torch.no_grad():
            for batch in tqdm(eval_dataloader, desc="Evaluating"):
                # Move batch to device
                batch = {k: v.to(self.device) if isinstance(v, torch.Tensor) else v 
                        for k, v in batch.items()}
                
                # Forward pass
                outputs = model(**batch)
                total_loss += outputs.loss.item()
                
                # Collect predictions
                if compute_generation_metrics:
                    # Generate text
                    generated = self._generate_text(
                        model, 
                        batch['input_ids'], 
                        max_length=max_generation_length
                    )
                    all_generated_texts.extend(generated)
                    
                    # Get reference texts
                    references = self._decode_batch(batch['labels'])
                    all_references.extend(references)
                    
                # Collect intent/domain predictions if available
                if hasattr(outputs, 'intent_logits'):
                    intent_preds = outputs.intent_logits.argmax(dim=-1)
                    intent_predictions.extend(intent_preds.cpu().tolist())
                    if 'intent_labels' in batch:
                        intent_labels.extend(batch['intent_labels'].cpu().tolist())
                        
                if hasattr(outputs, 'domain_logits'):
                    domain_preds = outputs.domain_logits.argmax(dim=-1)
                    domain_predictions.extend(domain_preds.cpu().tolist())
                    if 'domain_labels' in batch:
                        domain_labels.extend(batch['domain_labels'].cpu().tolist())
                        
        # Calculate metrics
        avg_loss = total_loss / len(eval_dataloader)
        perplexity = math.exp(avg_loss)
        
        # Generation quality metrics
        generation_metrics = {}
        if compute_generation_metrics and all_generated_texts:
            generation_metrics = self._compute_generation_metrics(
                all_generated_texts, 
                all_references
            )
            
        # Classification metrics
        classification_metrics = {}
        if intent_predictions and intent_labels:
            classification_metrics['intent_accuracy'] = accuracy_score(
                intent_labels, intent_predictions
            )
        if domain_predictions and domain_labels:
            classification_metrics['domain_accuracy'] = accuracy_score(
                domain_labels, domain_predictions
            )
            
        # Create result
        result = EvaluationResult(
            perplexity=perplexity,
            loss=avg_loss,
            bleu_score=generation_metrics.get('bleu', 0.0),
            rouge_scores=generation_metrics.get('rouge', {}),
            meteor_score=generation_metrics.get('meteor', 0.0),
            distinct_1=generation_metrics.get('distinct_1', 0.0),
            distinct_2=generation_metrics.get('distinct_2', 0.0),
            self_bleu=generation_metrics.get('self_bleu', 0.0),
            coherence_score=generation_metrics.get('coherence', 0.0),
            consistency_score=generation_metrics.get('consistency', 0.0),
            intent_accuracy=classification_metrics.get('intent_accuracy'),
            domain_accuracy=classification_metrics.get('domain_accuracy'),
            num_samples=len(eval_dataloader.dataset)
        )
        
        return result
        
    def _generate_text(
        self,
        model: nn.Module,
        input_ids: torch.Tensor,
        max_length: int = 100
    ) -> List[str]:
        """Generate text from model"""
        generated_ids = model.generate(
            input_ids=input_ids,
            max_length=max_length,
            temperature=0.7,
            do_sample=True,
            top_p=0.9,
            pad_token_id=self.tokenizer.pad_token_id,
            eos_token_id=self.tokenizer.eos_token_id
        )
        
        generated_texts = self._decode_batch(generated_ids)
        return generated_texts
        
    def _decode_batch(self, token_ids: torch.Tensor) -> List[str]:
        """Decode batch of token IDs to text"""
        texts = []
        for ids in token_ids:
            # Remove padding
            ids = ids[ids != self.tokenizer.pad_token_id]
            text = self.tokenizer.decode(ids, skip_special_tokens=True)
            texts.append(text)
        return texts
        
    def _compute_generation_metrics(
        self,
        predictions: List[str],
        references: List[str]
    ) -> Dict[str, float]:
        """Compute text generation metrics"""
        metrics = {}
        
        # BLEU score
        metrics['bleu'] = self._compute_bleu(predictions, references)
        
        # ROUGE scores
        metrics['rouge'] = self._compute_rouge(predictions, references)
        
        # Diversity metrics
        metrics['distinct_1'] = self._compute_distinct_n(predictions, 1)
        metrics['distinct_2'] = self._compute_distinct_n(predictions, 2)
        
        # Self-BLEU (diversity between generated samples)
        metrics['self_bleu'] = self._compute_self_bleu(predictions)
        
        # Coherence and consistency (simplified versions)
        metrics['coherence'] = self._compute_coherence(predictions)
        metrics['consistency'] = self._compute_consistency(predictions, references)
        
        return metrics
        
    def _compute_bleu(
        self,
        predictions: List[str],
        references: List[str]
    ) -> float:
        """Compute BLEU score"""
        if not predictions or not references:
            return 0.0
            
        smoothing = SmoothingFunction().method4
        scores = []
        
        for pred, ref in zip(predictions, references):
            pred_tokens = pred.split()
            ref_tokens = ref.split()
            
            if pred_tokens and ref_tokens:
                score = sentence_bleu(
                    [ref_tokens],
                    pred_tokens,
                    weights=(0.25, 0.25, 0.25, 0.25),
                    smoothing_function=smoothing
                )
                scores.append(score)
                
        return np.mean(scores) if scores else 0.0
        
    def _compute_rouge(
        self,
        predictions: List[str],
        references: List[str]
    ) -> Dict[str, float]:
        """Compute ROUGE scores"""
        if not predictions or not references:
            return {'rouge-1': 0.0, 'rouge-2': 0.0, 'rouge-l': 0.0}
            
        try:
            scores = self.rouge.get_scores(predictions, references, avg=True)
            return {
                'rouge-1': scores['rouge-1']['f'],
                'rouge-2': scores['rouge-2']['f'],
                'rouge-l': scores['rouge-l']['f']
            }
        except Exception:
            return {'rouge-1': 0.0, 'rouge-2': 0.0, 'rouge-l': 0.0}
            
    def _compute_distinct_n(self, texts: List[str], n: int) -> float:
        """Compute distinct-n metric for diversity"""
        if not texts:
            return 0.0
            
        ngrams = []
        for text in texts:
            tokens = text.split()
            ngrams.extend(zip(*[tokens[i:] for i in range(n)]))
            
        if not ngrams:
            return 0.0
            
        return len(set(ngrams)) / len(ngrams)
        
    def _compute_self_bleu(self, texts: List[str], sample_size: int = 100) -> float:
        """Compute self-BLEU for diversity measurement"""
        if len(texts) < 2:
            return 0.0
            
        # Sample texts if too many
        if len(texts) > sample_size:
            texts = np.random.choice(texts, sample_size, replace=False).tolist()
            
        scores = []
        smoothing = SmoothingFunction().method4
        
        for i, text in enumerate(texts):
            # Use all other texts as references
            references = texts[:i] + texts[i+1:]
            text_tokens = text.split()
            
            if text_tokens:
                # Compute BLEU against each reference
                text_scores = []
                for ref in references[:5]:  # Limit references for efficiency
                    ref_tokens = ref.split()
                    if ref_tokens:
                        score = sentence_bleu(
                            [ref_tokens],
                            text_tokens,
                            weights=(0.25, 0.25, 0.25, 0.25),
                            smoothing_function=smoothing
                        )
                        text_scores.append(score)
                        
                if text_scores:
                    scores.append(np.mean(text_scores))
                    
        return np.mean(scores) if scores else 0.0
        
    def _compute_coherence(self, texts: List[str]) -> float:
        """Compute coherence score (simplified)"""
        if not texts:
            return 0.0
            
        scores = []
        for text in texts:
            sentences = nltk.sent_tokenize(text)
            if len(sentences) > 1:
                # Simple coherence: check for topic continuity
                score = self._sentence_similarity(sentences)
                scores.append(score)
                
        return np.mean(scores) if scores else 0.5
        
    def _compute_consistency(
        self,
        predictions: List[str],
        references: List[str]
    ) -> float:
        """Compute consistency score"""
        if not predictions or not references:
            return 0.0
            
        scores = []
        for pred, ref in zip(predictions, references):
            # Check for consistent entity usage
            pred_entities = self._extract_entities(pred)
            ref_entities = self._extract_entities(ref)
            
            if ref_entities:
                overlap = len(pred_entities & ref_entities)
                score = overlap / len(ref_entities)
                scores.append(score)
                
        return np.mean(scores) if scores else 0.0
        
    def _sentence_similarity(self, sentences: List[str]) -> float:
        """Compute similarity between consecutive sentences"""
        if len(sentences) < 2:
            return 1.0
            
        similarities = []
        for i in range(len(sentences) - 1):
            # Simple word overlap similarity
            words1 = set(sentences[i].lower().split())
            words2 = set(sentences[i+1].lower().split())
            
            if words1 and words2:
                overlap = len(words1 & words2)
                similarity = overlap / max(len(words1), len(words2))
                similarities.append(similarity)
                
        return np.mean(similarities) if similarities else 0.0
        
    def _extract_entities(self, text: str) -> set:
        """Extract entities from text (simplified)"""
        # Extract capitalized words as potential entities
        entities = set()
        words = text.split()
        
        for word in words:
            if word and word[0].isupper() and len(word) > 1:
                entities.add(word.lower())
                
        return entities

class HumanEvaluationMetrics:
    """Metrics that simulate human evaluation criteria"""
    
    def __init__(self):
        self.fluency_keywords = {
            'positive': ['well', 'clearly', 'effectively', 'properly'],
            'negative': ['unclear', 'confusing', 'awkward', 'difficult']
        }
        
    def evaluate_fluency(self, text: str) -> float:
        """Evaluate text fluency"""
        sentences = nltk.sent_tokenize(text)
        if not sentences:
            return 0.0
            
        scores = []
        for sentence in sentences:
            # Check sentence length (too short or too long is bad)
            words = sentence.split()
            if 5 <= len(words) <= 30:
                score = 1.0
            else:
                score = 0.5
                
            # Check for repeated words
            word_counts = Counter(words)
            max_repeat = max(word_counts.values())
            if max_repeat > 2:
                score *= 0.8
                
            scores.append(score)
            
        return np.mean(scores)
        
    def evaluate_relevance(
        self,
        response: str,
        context: str,
        query: Optional[str] = None
    ) -> float:
        """Evaluate response relevance to context/query"""
        response_words = set(response.lower().split())
        context_words = set(context.lower().split())
        
        if query:
            query_words = set(query.lower().split())
            context_words.update(query_words)
            
        if not context_words:
            return 0.5
            
        # Calculate word overlap
        overlap = len(response_words & context_words)
        relevance = overlap / len(context_words)
        
        return min(relevance * 2, 1.0)  # Scale up but cap at 1.0
        
    def evaluate_engagement(self, text: str) -> float:
        """Evaluate how engaging the response is"""
        indicators = {
            'questions': ['?', 'what', 'how', 'why', 'when', 'where'],
            'personal': ['you', 'your', 'we', 'our'],
            'emotional': ['feel', 'think', 'believe', 'hope', 'wish'],
            'interactive': ['let me', 'let\'s', 'shall we', 'would you']
        }
        
        text_lower = text.lower()
        score = 0.0
        
        # Check for engagement indicators
        for category, keywords in indicators.items():
            if any(keyword in text_lower for keyword in keywords):
                score += 0.25
                
        return min(score, 1.0)

class DomainSpecificEvaluator:
    """Evaluator for domain-specific performance"""
    
    def __init__(self, domains: List[str]):
        self.domains = domains
        self.domain_patterns = self._load_domain_patterns()
        
    def _load_domain_patterns(self) -> Dict[str, List[str]]:
        """Load domain-specific patterns"""
        # Example patterns - in practice, load from configuration
        return {
            'medical': [
                r'\b(symptom|diagnosis|treatment|medication)\b',
                r'\b(patient|doctor|health|medical)\b'
            ],
            'technical': [
                r'\b(error|bug|issue|problem|solution)\b',
                r'\b(computer|software|hardware|system)\b'
            ],
            'customer_service': [
                r'\b(help|assist|support|service)\b',
                r'\b(customer|client|user|account)\b'
            ]
        }
        
    def evaluate_domain_accuracy(
        self,
        responses: List[str],
        true_domains: List[str]
    ) -> Dict[str, float]:
        """Evaluate domain classification accuracy"""
        predictions = []
        
        for response in responses:
            predicted_domain = self._predict_domain(response)
            predictions.append(predicted_domain)
            
        # Calculate per-domain metrics
        metrics = {}
        for domain in self.domains:
            domain_true = [d == domain for d in true_domains]
            domain_pred = [d == domain for d in predictions]
            
            if any(domain_true):
                precision, recall, f1, _ = precision_recall_fscore_support(
                    domain_true, domain_pred, average='binary'
                )
                metrics[f'{domain}_precision'] = precision
                metrics[f'{domain}_recall'] = recall
                metrics[f'{domain}_f1'] = f1
                
        # Overall accuracy
        metrics['overall_accuracy'] = accuracy_score(true_domains, predictions)
        
        return metrics
        
    def _predict_domain(self, text: str) -> str:
        """Predict domain from text"""
        text_lower = text.lower()
        scores = {}
        
        for domain, patterns in self.domain_patterns.items():
            score = 0
            for pattern in patterns:
                if re.search(pattern, text_lower):
                    score += 1
            scores[domain] = score
            
        if scores:
            return max(scores.items(), key=lambda x: x[1])[0]
        return 'general'
        
    def evaluate_domain_specific_quality(
        self,
        responses: List[str],
        domains: List[str]
    ) -> Dict[str, float]:
        """Evaluate quality within each domain"""
        domain_responses = defaultdict(list)
        
        # Group by domain
        for response, domain in zip(responses, domains):
            domain_responses[domain].append(response)
            
        metrics = {}
        for domain, domain_texts in domain_responses.items():
            # Check for domain-specific requirements
            if domain == 'medical':
                metrics[f'{domain}_safety'] = self._evaluate_medical_safety(domain_texts)
            elif domain == 'technical':
                metrics[f'{domain}_accuracy'] = self._evaluate_technical_accuracy(domain_texts)
            elif domain == 'customer_service':
                metrics[f'{domain}_politeness'] = self._evaluate_politeness(domain_texts)
                
        return metrics
        
    def _evaluate_medical_safety(self, texts: List[str]) -> float:
        """Evaluate medical response safety"""
        unsafe_patterns = [
            r'\b(diagnose|diagnosis is)\b',
            r'\b(definitely|certainly) have\b',
            r'\b(stop taking|discontinue) medication\b'
        ]
        
        safe_count = 0
        for text in texts:
            text_lower = text.lower()
            is_safe = not any(re.search(pattern, text_lower) for pattern in unsafe_patterns)
            if is_safe:
                safe_count += 1
                
        return safe_count / len(texts) if texts else 0.0
        
    def _evaluate_technical_accuracy(self, texts: List[str]) -> float:
        """Evaluate technical response accuracy"""
        # Simplified - check for technical terms usage
        technical_terms = ['restart', 'update', 'reinstall', 'configure', 'troubleshoot']
        
        scores = []
        for text in texts:
            text_lower = text.lower()
            term_count = sum(1 for term in technical_terms if term in text_lower)
            score = min(term_count / 3, 1.0)  # Expect at least 3 technical terms
            scores.append(score)
            
        return np.mean(scores) if scores else 0.0
        
    def _evaluate_politeness(self, texts: List[str]) -> float:
        """Evaluate customer service politeness"""
        polite_indicators = [
            'please', 'thank you', 'sorry', 'apologize',
            'would you', 'could you', 'may i', 'glad to help'
        ]
        
        scores = []
        for text in texts:
            text_lower = text.lower()
            indicator_count = sum(1 for indicator in polite_indicators if indicator in text_lower)
            score = min(indicator_count / 2, 1.0)  # Expect at least 2 polite indicators
            scores.append(score)
            
        return np.mean(scores) if scores else 0.0

# Example usage
if __name__ == "__main__":
    # Create evaluator
    tokenizer = AutoTokenizer.from_pretrained("gpt2")
    evaluator = ModelEvaluator(tokenizer)
    
    # Example evaluation data
    predictions = [
        "Hello! How can I help you today?",
        "The weather is nice and sunny.",
        "I understand your concern. Let me help you with that."
    ]
    
    references = [
        "Hi! What can I do for you?",
        "It's a beautiful sunny day.",
        "I see what you mean. I'll assist you with this issue."
    ]
    
    # Compute metrics
    metrics = evaluator._compute_generation_metrics(predictions, references)
    print("Generation Metrics:")
    print(f"BLEU: {metrics['bleu']:.3f}")
    print(f"ROUGE-1: {metrics['rouge']['rouge-1']:.3f}")
    print(f"Distinct-1: {metrics['distinct_1']:.3f}")
    print(f"Distinct-2: {metrics['distinct_2']:.3f}")
    
    # Human evaluation
    human_eval = HumanEvaluationMetrics()
    fluency = human_eval.evaluate_fluency(predictions[0])
    engagement = human_eval.evaluate_engagement(predictions[0])
    print(f"\nHuman-like Metrics:")
    print(f"Fluency: {fluency:.3f}")
    print(f"Engagement: {engagement:.3f}")