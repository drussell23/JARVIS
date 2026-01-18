import numpy as np
from typing import List, Dict, Optional, Tuple, Any, Union, Callable
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
import pickle
import random
import heapq
from pathlib import Path
import faiss
import torch
from sentence_transformers import SentenceTransformer
from transformers import AutoTokenizer, AutoModel
import chromadb
from chromadb.config import Settings
import tiktoken
from abc import ABC, abstractmethod
import asyncio
import aiofiles
from collections import defaultdict
from contextlib import asynccontextmanager
import re
import logging
import time
from enum import Enum
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# Logger for RAG engine
logger = logging.getLogger(__name__)

@dataclass
class Document:
    """Represents a document in the knowledge base"""
    id: str
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[np.ndarray] = None
    chunks: List['DocumentChunk'] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    
    
@dataclass
class DocumentChunk:
    """Represents a chunk of a document"""
    id: str
    document_id: str
    content: str
    embedding: Optional[np.ndarray] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    start_idx: int = 0
    end_idx: int = 0
    
    
@dataclass
class SearchResult:
    """Represents a search result"""
    chunk: DocumentChunk
    score: float
    document: Optional[Document] = None
    
    
@dataclass
class ConversationSummary:
    """Represents a conversation summary"""
    id: str
    conversation_id: str
    summary: str
    key_points: List[str]
    entities: List[Dict[str, str]]
    topics: List[str]
    sentiment: Dict[str, float]
    created_at: datetime = field(default_factory=datetime.now)

class TextChunker:
    """Splits text into overlapping chunks for better retrieval"""
    
    def __init__(self, chunk_size: int = 512, overlap: int = 128):
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.tokenizer = tiktoken.get_encoding("cl100k_base")
        
    def chunk_text(self, text: str, metadata: Dict[str, Any] = None) -> List[DocumentChunk]:
        """Split text into chunks with overlap"""
        # Tokenize the text
        tokens = self.tokenizer.encode(text)
        
        chunks = []
        doc_id = hashlib.md5(text.encode()).hexdigest()
        
        # Create chunks with overlap
        for i in range(0, len(tokens), self.chunk_size - self.overlap):
            chunk_tokens = tokens[i:i + self.chunk_size]
            
            # Find actual character positions
            chunk_text = self.tokenizer.decode(chunk_tokens)
            start_idx = text.find(chunk_text[:50])  # Find approximate start
            end_idx = start_idx + len(chunk_text)
            
            chunk = DocumentChunk(
                id=f"{doc_id}_{i}",
                document_id=doc_id,
                content=chunk_text,
                metadata=metadata or {},
                start_idx=start_idx,
                end_idx=end_idx
            )
            chunks.append(chunk)
            
        return chunks
        
    def chunk_by_sentences(self, text: str, max_chunk_size: int = 512) -> List[DocumentChunk]:
        """Split text into chunks by sentences"""
        # Simple sentence splitting
        sentences = re.split(r'(?<=[.!?])\s+', text)
        
        chunks = []
        current_chunk = []
        current_size = 0
        doc_id = hashlib.md5(text.encode()).hexdigest()
        chunk_idx = 0
        
        for sentence in sentences:
            sentence_tokens = len(self.tokenizer.encode(sentence))
            
            if current_size + sentence_tokens > max_chunk_size and current_chunk:
                # Create chunk
                chunk_text = ' '.join(current_chunk)
                chunk = DocumentChunk(
                    id=f"{doc_id}_{chunk_idx}",
                    document_id=doc_id,
                    content=chunk_text,
                    metadata={}
                )
                chunks.append(chunk)
                
                # Start new chunk with overlap (last sentence)
                current_chunk = [current_chunk[-1]] if len(current_chunk) > 1 else []
                current_size = len(self.tokenizer.encode(current_chunk[0])) if current_chunk else 0
                chunk_idx += 1
                
            current_chunk.append(sentence)
            current_size += sentence_tokens
            
        # Add final chunk
        if current_chunk:
            chunk_text = ' '.join(current_chunk)
            chunk = DocumentChunk(
                id=f"{doc_id}_{chunk_idx}",
                document_id=doc_id,
                content=chunk_text,
                metadata={}
            )
            chunks.append(chunk)
            
        return chunks

class EmbeddingModel:
    """Handles text embeddings for semantic search"""
    
    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2"):
        self.model = SentenceTransformer(model_name)
        self.embedding_dim = self.model.get_sentence_embedding_dimension()
        
    def embed_text(self, text: Union[str, List[str]]) -> np.ndarray:
        """Generate embeddings for text"""
        if isinstance(text, str):
            text = [text]
            
        embeddings = self.model.encode(text, convert_to_numpy=True)
        return embeddings
        
    def embed_batch(self, texts: List[str], batch_size: int = 32) -> np.ndarray:
        """Embed texts in batches"""
        all_embeddings = []
        
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            embeddings = self.embed_text(batch)
            all_embeddings.append(embeddings)
            
        return np.vstack(all_embeddings)

class VectorStore(ABC):
    """Abstract base class for vector stores"""
    
    @abstractmethod
    async def add_documents(self, documents: List[Document]):
        """Add documents to the store"""
        pass
        
    @abstractmethod
    async def search(self, query: str, k: int = 5) -> List[SearchResult]:
        """Search for similar documents"""
        pass
        
    @abstractmethod
    async def delete_document(self, doc_id: str):
        """Delete a document"""
        pass

class FAISSVectorStore(VectorStore):
    """FAISS-based vector store for efficient similarity search"""
    
    def __init__(self, embedding_model: EmbeddingModel, index_path: Optional[str] = None):
        self.embedding_model = embedding_model
        self.index_path = index_path or "faiss_index"
        self.documents: Dict[str, Document] = {}
        self.chunks: Dict[str, DocumentChunk] = {}
        
        # Initialize or load index
        if os.path.exists(f"{self.index_path}.index"):
            self.index = faiss.read_index(f"{self.index_path}.index")
            self._load_metadata()
        else:
            self.index = faiss.IndexFlatL2(embedding_model.embedding_dim)
            
    async def add_documents(self, documents: List[Document]):
        """Add documents to the FAISS index"""
        all_chunks = []
        all_embeddings = []
        
        for doc in documents:
            # Store document
            self.documents[doc.id] = doc
            
            # Process chunks
            for chunk in doc.chunks:
                if chunk.embedding is None:
                    chunk.embedding = self.embedding_model.embed_text(chunk.content)[0]
                    
                self.chunks[chunk.id] = chunk
                all_chunks.append(chunk)
                all_embeddings.append(chunk.embedding)
                
        # Add to FAISS index
        if all_embeddings:
            embeddings_array = np.array(all_embeddings).astype('float32')
            self.index.add(embeddings_array)
            
        # Save index and metadata
        await self._save()
        
    async def search(self, query: str, k: int = 5) -> List[SearchResult]:
        """Search for similar chunks"""
        # Generate query embedding
        query_embedding = self.embedding_model.embed_text(query)[0].reshape(1, -1).astype('float32')
        
        # Search in FAISS
        distances, indices = self.index.search(query_embedding, k)
        
        # Get chunks and create results
        results = []
        chunk_list = list(self.chunks.values())
        
        for idx, distance in zip(indices[0], distances[0]):
            if idx < len(chunk_list):
                chunk = chunk_list[idx]
                score = 1 / (1 + distance)  # Convert distance to similarity score
                
                # Get parent document
                document = self.documents.get(chunk.document_id)
                
                result = SearchResult(
                    chunk=chunk,
                    score=float(score),
                    document=document
                )
                results.append(result)
                
        return results
        
    async def delete_document(self, doc_id: str):
        """Delete a document and its chunks"""
        if doc_id in self.documents:
            # Remove chunks
            doc = self.documents[doc_id]
            for chunk in doc.chunks:
                if chunk.id in self.chunks:
                    del self.chunks[chunk.id]
                    
            # Remove document
            del self.documents[doc_id]
            
            # Rebuild index
            await self._rebuild_index()
            
    async def _rebuild_index(self):
        """Rebuild the FAISS index"""
        self.index = faiss.IndexFlatL2(self.embedding_model.embedding_dim)
        
        all_embeddings = []
        for chunk in self.chunks.values():
            if chunk.embedding is not None:
                all_embeddings.append(chunk.embedding)
                
        if all_embeddings:
            embeddings_array = np.array(all_embeddings).astype('float32')
            self.index.add(embeddings_array)
            
        await self._save()
        
    async def _save(self):
        """Save index and metadata"""
        # Save FAISS index
        faiss.write_index(self.index, f"{self.index_path}.index")
        
        # Save metadata
        metadata = {
            'documents': self.documents,
            'chunks': self.chunks
        }
        
        async with aiofiles.open(f"{self.index_path}.meta", 'wb') as f:
            await f.write(pickle.dumps(metadata))
            
    def _load_metadata(self):
        """Load metadata"""
        with open(f"{self.index_path}.meta", 'rb') as f:
            metadata = pickle.load(f)
            self.documents = metadata['documents']
            self.chunks = metadata['chunks']

class ChromaVectorStore(VectorStore):
    """ChromaDB-based vector store"""
    
    def __init__(self, embedding_model: EmbeddingModel, collection_name: str = "knowledge_base"):
        self.embedding_model = embedding_model
        self.client = chromadb.Client(Settings(
            chroma_db_impl="duckdb+parquet",
            persist_directory="./chroma_db"
        ))
        self.collection = self.client.get_or_create_collection(collection_name)
        
    async def add_documents(self, documents: List[Document]):
        """Add documents to ChromaDB"""
        for doc in documents:
            for chunk in doc.chunks:
                # Generate embedding if needed
                if chunk.embedding is None:
                    chunk.embedding = self.embedding_model.embed_text(chunk.content)[0]
                    
                # Add to collection
                self.collection.add(
                    embeddings=[chunk.embedding.tolist()],
                    documents=[chunk.content],
                    metadatas=[{
                        "document_id": chunk.document_id,
                        "chunk_id": chunk.id,
                        **chunk.metadata
                    }],
                    ids=[chunk.id]
                )
                
    async def search(self, query: str, k: int = 5) -> List[SearchResult]:
        """Search in ChromaDB"""
        query_embedding = self.embedding_model.embed_text(query)[0]
        
        results = self.collection.query(
            query_embeddings=[query_embedding.tolist()],
            n_results=k
        )
        
        search_results = []
        for i in range(len(results['ids'][0])):
            chunk = DocumentChunk(
                id=results['ids'][0][i],
                document_id=results['metadatas'][0][i]['document_id'],
                content=results['documents'][0][i],
                metadata=results['metadatas'][0][i]
            )
            
            score = 1 / (1 + results['distances'][0][i])
            
            search_results.append(SearchResult(
                chunk=chunk,
                score=score
            ))
            
        return search_results
        
    async def delete_document(self, doc_id: str):
        """Delete document from ChromaDB"""
        # Get all chunks for document
        results = self.collection.get(
            where={"document_id": doc_id}
        )
        
        if results['ids']:
            self.collection.delete(ids=results['ids'])

class KnowledgeBase:
    """Main knowledge base system with multiple stores and retrieval strategies"""
    
    def __init__(self, embedding_model: Optional[EmbeddingModel] = None):
        self.embedding_model = embedding_model or EmbeddingModel()
        self.vector_store = FAISSVectorStore(self.embedding_model)
        self.chunker = TextChunker()
        self.documents: Dict[str, Document] = {}
        
        # Additional indexes
        self.tfidf_vectorizer = TfidfVectorizer(max_features=5000)
        self.tfidf_matrix = None
        self.doc_index_mapping = {}
        
    async def add_document(self, content: str, metadata: Dict[str, Any] = None) -> Document:
        """Add a document to the knowledge base"""
        # Create document
        doc_id = hashlib.md5(content.encode()).hexdigest()
        
        # Chunk the document
        chunks = self.chunker.chunk_text(content, metadata)
        
        # Create document object
        document = Document(
            id=doc_id,
            content=content,
            metadata=metadata or {},
            chunks=chunks
        )
        
        # Store document
        self.documents[doc_id] = document
        
        # Add to vector store
        await self.vector_store.add_documents([document])
        
        # Update TF-IDF index
        await self._update_tfidf_index()
        
        return document
        
    async def add_documents_batch(self, documents: List[Tuple[str, Dict[str, Any]]]) -> List[Document]:
        """Add multiple documents in batch"""
        added_docs = []
        
        for content, metadata in documents:
            doc = await self.add_document(content, metadata)
            added_docs.append(doc)
            
        return added_docs
        
    async def search(self, query: str, k: int = 5, strategy: str = "hybrid") -> List[SearchResult]:
        """
        Search the knowledge base
        
        Args:
            query: Search query
            k: Number of results
            strategy: Search strategy (semantic, keyword, hybrid)
        """
        if strategy == "semantic":
            return await self._semantic_search(query, k)
        elif strategy == "keyword":
            return await self._keyword_search(query, k)
        elif strategy == "hybrid":
            return await self._hybrid_search(query, k)
        else:
            raise ValueError(f"Unknown search strategy: {strategy}")
            
    async def _semantic_search(self, query: str, k: int) -> List[SearchResult]:
        """Semantic search using embeddings"""
        return await self.vector_store.search(query, k)
        
    async def _keyword_search(self, query: str, k: int) -> List[SearchResult]:
        """Keyword search using TF-IDF"""
        if self.tfidf_matrix is None:
            return []
            
        # Transform query
        query_vector = self.tfidf_vectorizer.transform([query])
        
        # Calculate similarities
        similarities = cosine_similarity(query_vector, self.tfidf_matrix).flatten()
        
        # Get top k results
        top_indices = similarities.argsort()[-k:][::-1]
        
        results = []
        for idx in top_indices:
            if idx in self.doc_index_mapping:
                chunk_id = self.doc_index_mapping[idx]
                chunk = self.vector_store.chunks.get(chunk_id)
                
                if chunk:
                    doc = self.documents.get(chunk.document_id)
                    results.append(SearchResult(
                        chunk=chunk,
                        score=float(similarities[idx]),
                        document=doc
                    ))
                    
        return results
        
    async def _hybrid_search(self, query: str, k: int) -> List[SearchResult]:
        """Hybrid search combining semantic and keyword search"""
        # Get results from both methods
        semantic_results = await self._semantic_search(query, k)
        keyword_results = await self._keyword_search(query, k)
        
        # Combine and re-rank
        combined_results = {}
        
        # Add semantic results
        for result in semantic_results:
            combined_results[result.chunk.id] = result
            
        # Merge keyword results
        for result in keyword_results:
            if result.chunk.id in combined_results:
                # Average the scores
                combined_results[result.chunk.id].score = (
                    combined_results[result.chunk.id].score + result.score
                ) / 2
            else:
                combined_results[result.chunk.id] = result
                
        # Sort by score and return top k
        sorted_results = sorted(
            combined_results.values(),
            key=lambda x: x.score,
            reverse=True
        )
        
        return sorted_results[:k]
        
    async def _update_tfidf_index(self):
        """Update TF-IDF index"""
        # Get all chunk texts
        texts = []
        chunk_ids = []
        
        for chunk_id, chunk in self.vector_store.chunks.items():
            texts.append(chunk.content)
            chunk_ids.append(chunk_id)
            
        if texts:
            # Fit or transform TF-IDF
            self.tfidf_matrix = self.tfidf_vectorizer.fit_transform(texts)
            
            # Update mapping
            self.doc_index_mapping = {i: chunk_id for i, chunk_id in enumerate(chunk_ids)}
            
    async def get_relevant_context(self, query: str, max_tokens: int = 2048) -> str:
        """Get relevant context for a query within token limit"""
        # Search for relevant chunks
        results = await self.search(query, k=10)
        
        # Build context within token limit
        context_parts = []
        current_tokens = 0
        tokenizer = self.chunker.tokenizer
        
        for result in results:
            chunk_tokens = len(tokenizer.encode(result.chunk.content))
            
            if current_tokens + chunk_tokens <= max_tokens:
                context_parts.append(result.chunk.content)
                current_tokens += chunk_tokens
            else:
                # Truncate if needed
                remaining_tokens = max_tokens - current_tokens
                if remaining_tokens > 100:  # Only add if meaningful
                    truncated = tokenizer.decode(
                        tokenizer.encode(result.chunk.content)[:remaining_tokens]
                    )
                    context_parts.append(truncated)
                break
                
        return "\n\n".join(context_parts)
        
    async def update_document(self, doc_id: str, content: str, metadata: Dict[str, Any] = None):
        """Update an existing document"""
        # Delete old version
        await self.vector_store.delete_document(doc_id)
        
        # Add new version
        await self.add_document(content, metadata)
        
    async def delete_document(self, doc_id: str):
        """Delete a document"""
        if doc_id in self.documents:
            del self.documents[doc_id]
            await self.vector_store.delete_document(doc_id)
            await self._update_tfidf_index()

class ConversationSummarizer:
    """Summarizes conversations for long-term memory"""
    
    def __init__(self, model_name: str = "facebook/bart-large-cnn"):
        from transformers import pipeline
        self.entity_extractor = pipeline("ner", model="dslim/bert-base-NER")
        
    def summarize_conversation(self, messages: List[Dict[str, str]], max_length: int = 150) -> ConversationSummary:
        """Summarize a conversation"""
        # Combine messages
        conversation_text = "\n".join([
            f"{msg['role']}: {msg['content']}" for msg in messages
        ])
        
        # Generate summary
        summary_result = self.summarizer(
            conversation_text,
            max_length=max_length,
            min_length=30,
            do_sample=False
        )
        summary_text = summary_result[0]['summary_text']
        
        # Extract key points
        key_points = self._extract_key_points(messages)
        
        # Extract entities
        entities = self._extract_entities(conversation_text)
        
        # Extract topics
        topics = self._extract_topics(conversation_text)
        
        # Analyze sentiment
        sentiment = self._analyze_sentiment(messages)
        
        # Create summary object
        conv_id = hashlib.md5(conversation_text.encode()).hexdigest()
        summary = ConversationSummary(
            id=f"summary_{conv_id}",
            conversation_id=conv_id,
            summary=summary_text,
            key_points=key_points,
            entities=entities,
            topics=topics,
            sentiment=sentiment
        )
        
        return summary
        
    def _extract_key_points(self, messages: List[Dict[str, str]]) -> List[str]:
        """Extract key points from conversation"""
        key_points = []
        
        # Look for questions and their answers
        for i, msg in enumerate(messages):
            if msg['role'] == 'user' and '?' in msg['content']:
                # This is a question
                if i + 1 < len(messages) and messages[i + 1]['role'] == 'assistant':
                    # Found Q&A pair
                    key_points.append(f"Q: {msg['content'][:100]}... A: {messages[i + 1]['content'][:100]}...")
                    
        return key_points[:5]  # Limit to 5 key points
        
    def _extract_entities(self, text: str) -> List[Dict[str, str]]:
        """Extract named entities"""
        entities = self.entity_extractor(text)
        
        # Group by entity type
        grouped_entities = defaultdict(list)
        for entity in entities:
            grouped_entities[entity['entity']].append(entity['word'])
            
        # Format results
        formatted_entities = []
        for entity_type, words in grouped_entities.items():
            formatted_entities.append({
                'type': entity_type,
                'values': list(set(words))  # Unique values
            })
            
        return formatted_entities
        
    def _extract_topics(self, text: str) -> List[str]:
        """Extract main topics using simple keyword extraction"""
        # Simple topic extraction based on noun phrases
        # In production, use more sophisticated topic modeling
        from collections import Counter
        import nltk
        
        try:
            nltk.download('punkt', quiet=True)
            nltk.download('averaged_perceptron_tagger', quiet=True)
            nltk.download('stopwords', quiet=True)
            
            from nltk.tokenize import word_tokenize
            from nltk.tag import pos_tag
            from nltk.corpus import stopwords
            
            # Tokenize and tag
            tokens = word_tokenize(text.lower())
            pos_tags = pos_tag(tokens)
            
            # Extract nouns
            stop_words = set(stopwords.words('english'))
            nouns = [
                word for word, pos in pos_tags
                if pos in ['NN', 'NNS', 'NNP', 'NNPS'] and word not in stop_words and len(word) > 3
            ]
            
            # Count frequency
            noun_freq = Counter(nouns)
            
            # Return top topics
            return [noun for noun, _ in noun_freq.most_common(5)]
            
        except Exception:
            # Fallback to simple word frequency
            words = text.lower().split()
            word_freq = Counter(words)
            return [word for word, _ in word_freq.most_common(5) if len(word) > 4]
            
    def _analyze_sentiment(self, messages: List[Dict[str, str]]) -> Dict[str, float]:
        """Analyze conversation sentiment"""
        # Simple sentiment based on message content
        positive_words = {'good', 'great', 'excellent', 'happy', 'thanks', 'perfect'}
        negative_words = {'bad', 'poor', 'terrible', 'unhappy', 'problem', 'issue'}
        
        positive_count = 0
        negative_count = 0
        total_words = 0
        
        for msg in messages:
            words = set(msg['content'].lower().split())
            positive_count += len(words & positive_words)
            negative_count += len(words & negative_words)
            total_words += len(words)
            
        if total_words > 0:
            return {
                'positive': positive_count / total_words,
                'negative': negative_count / total_words,
                'neutral': 1 - (positive_count + negative_count) / total_words
            }
        else:
            return {'positive': 0.0, 'negative': 0.0, 'neutral': 1.0}

class LearningEngine:
    """Implements learning and adaptation capabilities"""
    
    def __init__(self, knowledge_base: KnowledgeBase):
        self.knowledge_base = knowledge_base
        self.user_preferences: Dict[str, Any] = {}
        self.interaction_history: List[Dict[str, Any]] = []
        self.feedback_scores: Dict[str, float] = {}
        self.learned_patterns: Dict[str, List[str]] = defaultdict(list)
        
    async def learn_from_interaction(self, user_input: str, response: str, feedback: Optional[float] = None):
        """Learn from a single interaction"""
        interaction = {
            'timestamp': datetime.now(),
            'user_input': user_input,
            'response': response,
            'feedback': feedback
        }
        
        self.interaction_history.append(interaction)
        
        # Learn patterns
        await self._learn_patterns(user_input, response)
        
        # Update preferences
        await self._update_preferences(user_input, response, feedback)
        
        # Store good interactions in knowledge base
        if feedback and feedback > 0.8:
            await self._store_successful_interaction(user_input, response)
            
    async def _learn_patterns(self, user_input: str, response: str):
        """Learn common patterns from interactions"""
        # Extract intent/pattern
        input_lower = user_input.lower()
        
        # Simple pattern matching
        patterns = {
            'greeting': ['hi', 'hello', 'hey', 'good morning'],
            'question': ['what', 'how', 'why', 'when', 'where'],
            'request': ['can you', 'could you', 'please', 'would you'],
            'feedback': ['thanks', 'thank you', 'great', 'good job']
        }
        
        for pattern_type, keywords in patterns.items():
            if any(keyword in input_lower for keyword in keywords):
                self.learned_patterns[pattern_type].append(response)
                
    async def _update_preferences(self, user_input: str, response: str, feedback: Optional[float]):
        """Update user preferences based on feedback"""
        if feedback is not None:
            # Extract features from the interaction
            features = {
                'response_length': len(response.split()),
                'formal_tone': 'please' in response.lower() or 'would' in response.lower(),
                'detailed': len(response) > 200,
                'technical': any(term in response.lower() for term in ['algorithm', 'function', 'system'])
            }
            
            # Update preferences based on feedback
            for feature, value in features.items():
                if feature not in self.user_preferences:
                    self.user_preferences[feature] = {'score': 0.5, 'count': 0}
                    
                # Update score with exponential moving average
                alpha = 0.3
                old_score = self.user_preferences[feature]['score']
                self.user_preferences[feature]['score'] = alpha * feedback + (1 - alpha) * old_score
                self.user_preferences[feature]['count'] += 1
                
    async def _store_successful_interaction(self, user_input: str, response: str):
        """Store successful interactions in knowledge base"""
        # Create a document from the interaction
        content = f"Q: {user_input}\nA: {response}"
        metadata = {
            'type': 'learned_interaction',
            'timestamp': datetime.now().isoformat(),
            'success_score': 0.9
        }
        
        await self.knowledge_base.add_document(content, metadata)
        
    def get_adapted_parameters(self) -> Dict[str, Any]:
        """Get adapted parameters based on learned preferences"""
        params = {}
        
        # Adapt response length
        if 'response_length' in self.user_preferences:
            pref = self.user_preferences['response_length']
            if pref['score'] > 0.7:
                params['max_length'] = 300  # Longer responses
            elif pref['score'] < 0.3:
                params['max_length'] = 100  # Shorter responses
                
        # Adapt formality
        if 'formal_tone' in self.user_preferences:
            pref = self.user_preferences['formal_tone']
            params['formal'] = pref['score'] > 0.5
            
        # Adapt detail level
        if 'detailed' in self.user_preferences:
            pref = self.user_preferences['detailed']
            params['detail_level'] = 'high' if pref['score'] > 0.6 else 'low'
            
        return params
        
    def get_personalized_prompt_additions(self) -> str:
        """Get personalized additions to system prompt based on learning"""
        additions = []
        
        # Add tone preferences
        if self.user_preferences.get('formal_tone', {}).get('score', 0.5) > 0.7:
            additions.append("Use a formal and professional tone.")
        elif self.user_preferences.get('formal_tone', {}).get('score', 0.5) < 0.3:
            additions.append("Use a casual and friendly tone.")
            
        # Add detail preferences
        if self.user_preferences.get('detailed', {}).get('score', 0.5) > 0.7:
            additions.append("Provide detailed and comprehensive answers.")
        elif self.user_preferences.get('detailed', {}).get('score', 0.5) < 0.3:
            additions.append("Keep answers concise and to the point.")
            
        return " ".join(additions)


# =============================================================================
# Trinity RAG Bridge - Connects Web Scraping Memory to RAG Pipeline
# =============================================================================

class CircuitState(Enum):
    """Circuit breaker states for resilient async operations."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"      # Failing, reject calls
    HALF_OPEN = "half_open"  # Testing if recovered


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker pattern."""
    failure_threshold: int = 3  # Failures before opening
    recovery_timeout_seconds: float = 30.0  # Time before half-open
    success_threshold: int = 2  # Successes to close from half-open


class TrinityRAGBridge:
    """
    Async bridge connecting Trinity Knowledge Indexer to RAG pipeline.
    
    The "Brain Bridge" that connects JARVIS's scraped web memory to his
    conversational abilities. Features:
    
    - Circuit breaker pattern for resilience
    - Parallel retrieval from multiple sources  
    - Graceful degradation when indexer unavailable
    - Source attribution formatting
    - Dynamic initialization (no hardcoded imports)
    
    Architecture:
        ┌──────────────────────────────────────────────┐
        │     TrinityRAGBridge                         │
        │  ┌────────────────┐  ┌────────────────────┐  │
        │  │ Circuit Breaker│  │ Trinity Indexer    │  │
        │  │ (Resilience)   │──│ (ChromaDB/FAISS)   │  │
        │  └────────────────┘  └────────────────────┘  │
        │            │                                 │
        │            ▼                                 │
        │  ┌────────────────────────────────────────┐  │
        │  │ Format: [Web Source: URL] content      │  │
        │  └────────────────────────────────────────┘  │
        └──────────────────────────────────────────────┘
    """
    
    def __init__(
        self,
        circuit_config: Optional[CircuitBreakerConfig] = None,
        search_timeout_seconds: float = 5.0,
        max_results: int = 5,
        min_similarity: float = 0.3,
    ):
        self._circuit_config = circuit_config or CircuitBreakerConfig()
        self._search_timeout = search_timeout_seconds
        self._max_results = max_results
        self._min_similarity = min_similarity
        
        # Circuit breaker state
        self._circuit_state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0  
        self._last_failure_time: Optional[float] = None
        
        # Lazy-loaded indexer reference
        self._indexer: Optional[Any] = None
        self._indexer_available: Optional[bool] = None
        
        logger.debug("[TrinityRAGBridge] Initialized with circuit breaker pattern")
    
    async def _get_indexer(self) -> Optional[Any]:
        """
        Lazy-load the Trinity Knowledge Indexer.
        
        Uses dynamic import to avoid circular dependencies and
        gracefully handles missing dependencies.
        """
        if self._indexer is not None:
            return self._indexer
            
        if self._indexer_available is False:
            return None  # Previously failed, don't retry in this session
            
        try:
            # Dynamic import prevents startup failures if Trinity not available
            from backend.autonomy.trinity_knowledge_indexer import get_knowledge_indexer
            self._indexer = await get_knowledge_indexer()
            self._indexer_available = True
            logger.info("[TrinityRAGBridge] ✅ Trinity Knowledge Indexer connected")
            return self._indexer
        except ImportError as e:
            logger.warning(f"[TrinityRAGBridge] Trinity Indexer not available: {e}")
            self._indexer_available = False
            return None
        except Exception as e:
            logger.error(f"[TrinityRAGBridge] Failed to initialize indexer: {e}")
            self._indexer_available = False
            return None
    
    def _check_circuit(self) -> bool:
        """
        Check if circuit breaker allows the call.
        
        Returns:
            True if call should proceed, False if rejected
        """
        if self._circuit_state == CircuitState.CLOSED:
            return True
            
        if self._circuit_state == CircuitState.OPEN:
            # Check if recovery timeout has elapsed
            if self._last_failure_time is not None:
                elapsed = time.time() - self._last_failure_time
                if elapsed >= self._circuit_config.recovery_timeout_seconds:
                    logger.info("[TrinityRAGBridge] Circuit transitioning to HALF_OPEN")
                    self._circuit_state = CircuitState.HALF_OPEN
                    self._success_count = 0
                    return True
            return False
            
        # HALF_OPEN state - allow the call for testing
        return True
    
    def _record_success(self):
        """Record successful call for circuit breaker."""
        if self._circuit_state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self._circuit_config.success_threshold:
                logger.info("[TrinityRAGBridge] Circuit CLOSED (recovered)")
                self._circuit_state = CircuitState.CLOSED
                self._failure_count = 0
        elif self._circuit_state == CircuitState.CLOSED:
            self._failure_count = 0  # Reset on success
    
    def _record_failure(self):
        """Record failed call for circuit breaker."""
        self._failure_count += 1
        self._last_failure_time = time.time()
        
        if self._circuit_state == CircuitState.HALF_OPEN:
            logger.warning("[TrinityRAGBridge] Circuit OPEN (failed during recovery)")
            self._circuit_state = CircuitState.OPEN
        elif self._failure_count >= self._circuit_config.failure_threshold:
            logger.warning(f"[TrinityRAGBridge] Circuit OPEN after {self._failure_count} failures")
            self._circuit_state = CircuitState.OPEN
    
    async def search(
        self,
        query: str,
        limit: Optional[int] = None,
        min_similarity: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search Trinity Knowledge Indexer for relevant web content.
        
        Args:
            query: Search query text
            limit: Maximum results (default from config)
            min_similarity: Minimum similarity score (default from config)
            
        Returns:
            List of results with text, metadata, score, formatted_source
        """
        if not self._check_circuit():
            logger.debug("[TrinityRAGBridge] Circuit OPEN, skipping Trinity search")
            return []
            
        try:
            indexer = await self._get_indexer()
            if indexer is None:
                return []
            
            # Execute search with timeout
            search_task = indexer.search_similar(
                query=query,
                limit=limit or self._max_results,
                min_similarity=min_similarity or self._min_similarity,
            )
            
            results = await asyncio.wait_for(
                search_task,
                timeout=self._search_timeout
            )
            
            # Format results with source attribution
            formatted_results = []
            for result in results:
                formatted = {
                    **result,
                    "formatted_source": self._format_web_source(
                        result.get("text", ""),
                        result.get("metadata", {})
                    )
                }
                formatted_results.append(formatted)
            
            self._record_success()
            logger.debug(f"[TrinityRAGBridge] Found {len(formatted_results)} web sources")
            return formatted_results
            
        except asyncio.TimeoutError:
            logger.warning(f"[TrinityRAGBridge] Search timed out after {self._search_timeout}s")
            self._record_failure()
            return []
        except Exception as e:
            logger.error(f"[TrinityRAGBridge] Search failed: {e}")
            self._record_failure()
            return []
    
    def _format_web_source(self, text: str, metadata: Dict[str, Any]) -> str:
        """
        Format web content with source attribution.
        
        Format: [Web Source: {url}] {content}
        """
        url = metadata.get("url", "unknown source")
        title = metadata.get("title", "")
        
        header = f"[Web Source: {url}]"
        if title:
            header = f"[Web Source: {title} - {url}]"
            
        return f"{header}\n{text}"
    
    def get_status(self) -> Dict[str, Any]:
        """Get bridge status for monitoring."""
        return {
            "circuit_state": self._circuit_state.value,
            "failure_count": self._failure_count,
            "indexer_available": self._indexer_available,
            "search_timeout": self._search_timeout,
            "max_results": self._max_results,
        }


# =============================================================================
# RAG Engine - Main Integration Point
# =============================================================================

class RAGEngine:
    """
    Main RAG engine integrating all components.
    
    v2.0: Now includes TrinityRAGBridge for web-scraped knowledge retrieval.
    Combines local knowledge base with Trinity indexer for comprehensive context.
    """
    
    def __init__(self, base_model_name: str = "gpt2"):
        self.embedding_model = EmbeddingModel()
        self.knowledge_base = KnowledgeBase(self.embedding_model)
        self.summarizer = ConversationSummarizer()
        self.learning_engine = LearningEngine(self.knowledge_base)
        self.base_model_name = base_model_name
        
        # v2.0: Trinity RAG Bridge for web knowledge
        self._trinity_bridge: Optional[TrinityRAGBridge] = None
        self._trinity_enabled = os.getenv("TRINITY_RAG_ENABLED", "true").lower() == "true"
        
    def _get_trinity_bridge(self) -> TrinityRAGBridge:
        """Lazy initialization of Trinity bridge."""
        if self._trinity_bridge is None:
            self._trinity_bridge = TrinityRAGBridge(
                search_timeout_seconds=float(os.getenv("TRINITY_SEARCH_TIMEOUT", "5.0")),
                max_results=int(os.getenv("TRINITY_MAX_RESULTS", "5")),
                min_similarity=float(os.getenv("TRINITY_MIN_SIMILARITY", "0.3")),
            )
        return self._trinity_bridge
        
    async def generate_with_retrieval(
        self,
        query: str,
        conversation_history: List[Dict[str, str]] = None,
        include_web_sources: bool = True,
    ) -> Dict[str, Any]:
        """
        Generate response using retrieval-augmented generation.
        
        v2.0: Now combines web-scraped content (Trinity) with local knowledge base.
        
        Args:
            query: User query
            conversation_history: Previous conversation messages
            include_web_sources: Whether to include Trinity web sources
            
        Returns:
            Dict with response, context_used, sources, web_sources
        """
        # Parallel retrieval from multiple sources
        trinity_context = ""
        web_sources = []
        local_context = ""
        
        # Create retrieval tasks
        tasks = []
        
        # Task 1: Local knowledge base retrieval (always)
        async def get_local_context():
            return await self.knowledge_base.get_relevant_context(query, max_tokens=768)
        tasks.append(get_local_context())
        
        # Task 2: Trinity web retrieval (if enabled)
        if include_web_sources and self._trinity_enabled:
            async def get_trinity_context():
                bridge = self._get_trinity_bridge()
                results = await bridge.search(query)
                return results
            tasks.append(get_trinity_context())
        
        # Execute in parallel
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Process local context (first result)
        if not isinstance(results[0], Exception):
            local_context = results[0]
        else:
            logger.warning(f"[RAGEngine] Local KB retrieval failed: {results[0]}")
            
        # Process Trinity context (second result if available)
        if len(results) > 1 and not isinstance(results[1], Exception):
            web_results = results[1]
            if web_results:
                web_sources = web_results
                # Build formatted web context (web sources first for recency)
                trinity_parts = [r.get("formatted_source", "") for r in web_results if r.get("formatted_source")]
                trinity_context = "\n\n".join(trinity_parts[:3])  # Limit to top 3
                
        # Combine contexts: Web sources first (more recent), then local KB
        combined_context = self._merge_contexts(trinity_context, local_context)
        
        # Get adapted parameters
        adapted_params = self.learning_engine.get_adapted_parameters()
        
        # Get personalized prompt additions
        prompt_additions = self.learning_engine.get_personalized_prompt_additions()
        
        # Build augmented prompt
        augmented_prompt = self._build_augmented_prompt(
            query,
            combined_context,
            conversation_history,
            prompt_additions
        )
        
        # Generate response (placeholder - integrate with actual model)
        response = f"Based on the context: {combined_context[:200]}... I can help you with: {query}"
        
        # Store interaction for learning
        await self.learning_engine.learn_from_interaction(query, response)
        
        return {
            'response': response,
            'context_used': combined_context,
            'adapted_parameters': adapted_params,
            'sources': await self._get_source_documents(query),
            'web_sources': web_sources,
            'trinity_status': self._get_trinity_bridge().get_status() if self._trinity_enabled else None,
        }
    
    def _merge_contexts(
        self,
        web_context: str,
        local_context: str,
        max_combined_tokens: int = 1500,
    ) -> str:
        """
        Merge web and local contexts with deduplication.
        
        Web sources come first (more recent/relevant scraped data),
        followed by local knowledge base content.
        """
        parts = []
        
        if web_context.strip():
            parts.append("=== Recent Web Knowledge ===")
            parts.append(web_context.strip())
            
        if local_context.strip():
            if parts:
                parts.append("\n=== Local Knowledge Base ===")
            parts.append(local_context.strip())
            
        combined = "\n".join(parts)
        
        # Simple token limit (rough estimate: 4 chars per token)
        max_chars = max_combined_tokens * 4
        if len(combined) > max_chars:
            combined = combined[:max_chars] + "..."
            
        return combined
        
    def _build_augmented_prompt(self, query: str, context: str, history: List[Dict[str, str]], additions: str) -> str:
        """Build prompt with retrieved context"""
        prompt_parts = []
        
        # System prompt with additions
        system_prompt = f"You are a helpful AI assistant with access to a knowledge base. {additions}"
        prompt_parts.append(f"System: {system_prompt}")
        
        # Add context
        if context:
            prompt_parts.append(f"\nRelevant Context:\n{context}")
            
        # Add conversation history
        if history:
            prompt_parts.append("\nConversation History:")
            for msg in history[-5:]:  # Last 5 messages
                prompt_parts.append(f"{msg['role']}: {msg['content']}")
                
        # Add current query
        prompt_parts.append(f"\nUser: {query}")
        prompt_parts.append("Assistant:")
        
        return "\n".join(prompt_parts)
        
    async def _get_source_documents(self, query: str, k: int = 3) -> List[Dict[str, Any]]:
        """Get source documents for attribution"""
        results = await self.knowledge_base.search(query, k=k)
        
        sources = []
        for result in results:
            if result.document:
                sources.append({
                    'id': result.document.id,
                    'content_preview': result.chunk.content[:200] + "...",
                    'score': result.score,
                    'metadata': result.document.metadata
                })
                
        return sources
        
    async def add_knowledge(self, content: str, metadata: Dict[str, Any] = None):
        """Add knowledge to the base"""
        return await self.knowledge_base.add_document(content, metadata)
        
    async def summarize_conversation(self, messages: List[Dict[str, str]]) -> ConversationSummary:
        """Summarize a conversation"""
        summary = self.summarizer.summarize_conversation(messages)
        
        # Store summary in knowledge base
        await self.knowledge_base.add_document(
            summary.summary,
            {
                'type': 'conversation_summary',
                'conversation_id': summary.conversation_id,
                'key_points': summary.key_points,
                'entities': summary.entities,
                'topics': summary.topics
            }
        )
        
        return summary
        
    async def provide_feedback(self, query: str, response: str, score: float):
        """Provide feedback for learning"""
        await self.learning_engine.learn_from_interaction(query, response, score)
        
    def get_learning_insights(self) -> Dict[str, Any]:
        """Get insights from learning engine"""
        return {
            'user_preferences': self.learning_engine.user_preferences,
            'total_interactions': len(self.learning_engine.interaction_history),
            'learned_patterns': dict(self.learning_engine.learned_patterns),
            'average_feedback': np.mean([
                i['feedback'] for i in self.learning_engine.interaction_history
                if i['feedback'] is not None
            ]) if any(i['feedback'] is not None for i in self.learning_engine.interaction_history) else None
        }


# =============================================================================
# Phase 1: Advanced Async Utilities
# =============================================================================

class AsyncLockWithTimeout:
    """
    Async lock with timeout, deadlock detection, and metrics.
    
    Features:
    - Configurable timeout (default from config)
    - Deadlock detection via waiter tracking
    - Lock acquisition metrics
    - Correlation ID for tracing
    
    Usage:
        lock = AsyncLockWithTimeout(timeout=5.0, name="my_lock")
        async with lock.acquire_with_context(correlation_id="abc123"):
            # Critical section
    """
    
    def __init__(
        self,
        timeout: Optional[float] = None,
        name: str = "unnamed_lock",
    ):
        self._lock = asyncio.Lock()
        self._timeout = timeout or float(os.getenv("ASYNC_LOCK_TIMEOUT", "5.0"))
        self._name = name
        self._waiters: Dict[str, float] = {}  # correlation_id -> wait_start_time
        self._acquisition_count = 0
        self._timeout_count = 0
        
    @asynccontextmanager
    async def acquire_with_context(
        self,
        correlation_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        """
        Acquire lock with timeout and context tracking.
        
        Args:
            correlation_id: Optional ID for tracing
            timeout: Override timeout for this acquisition
            
        Raises:
            asyncio.TimeoutError: If lock not acquired within timeout
        """
        effective_timeout = timeout or self._timeout
        corr_id = correlation_id or f"anon_{id(asyncio.current_task())}"
        wait_start = time.time()
        
        # Track waiter for deadlock detection
        self._waiters[corr_id] = wait_start
        
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=effective_timeout)
            self._acquisition_count += 1
            wait_time = time.time() - wait_start
            
            if wait_time > 1.0:
                logger.warning(
                    f"[AsyncLock:{self._name}] Slow lock acquisition: {wait_time:.2f}s "
                    f"(correlation_id={corr_id})"
                )
            
            try:
                yield
            finally:
                self._lock.release()
                
        except asyncio.TimeoutError:
            self._timeout_count += 1
            logger.error(
                f"[AsyncLock:{self._name}] Lock timeout after {effective_timeout}s "
                f"(correlation_id={corr_id}, waiters={len(self._waiters)})"
            )
            raise
        finally:
            self._waiters.pop(corr_id, None)
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get lock metrics for monitoring."""
        return {
            "name": self._name,
            "acquisition_count": self._acquisition_count,
            "timeout_count": self._timeout_count,
            "current_waiters": len(self._waiters),
            "is_locked": self._lock.locked(),
        }


# Import asynccontextmanager if not already imported
try:
    from contextlib import asynccontextmanager
except ImportError:
    pass  # Already imported above


@dataclass
class CorrelationContext:
    """
    W3C Trace Context for distributed tracing.
    
    Propagates correlation IDs across all service calls for:
    - Trinity Indexer searches
    - LLM API calls
    - Cross-repo calls (J-Prime, Reactor Core)
    
    Format follows W3C Trace Context specification.
    """
    trace_id: str  # 32 hex chars
    span_id: str   # 16 hex chars
    parent_span_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    baggage: Dict[str, str] = field(default_factory=dict)
    
    @classmethod
    def create(cls) -> "CorrelationContext":
        """Create a new trace root."""
        import uuid
        trace_id = uuid.uuid4().hex
        span_id = uuid.uuid4().hex[:16]
        return cls(trace_id=trace_id, span_id=span_id)
    
    def create_child_span(self) -> "CorrelationContext":
        """Create a child span for nested operations."""
        import uuid
        return CorrelationContext(
            trace_id=self.trace_id,
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=self.span_id,
            baggage=self.baggage.copy(),
        )
    
    def to_headers(self) -> Dict[str, str]:
        """Convert to W3C traceparent header format."""
        return {
            "traceparent": f"00-{self.trace_id}-{self.span_id}-01",
            "tracestate": ",".join(f"{k}={v}" for k, v in self.baggage.items()),
        }
    
    @classmethod
    def from_headers(cls, headers: Dict[str, str]) -> Optional["CorrelationContext"]:
        """Parse from W3C traceparent header."""
        traceparent = headers.get("traceparent", "")
        parts = traceparent.split("-")
        if len(parts) >= 3:
            return cls(trace_id=parts[1], span_id=parts[2])
        return None
    
    @property
    def short_id(self) -> str:
        """Short ID for logging."""
        return f"{self.trace_id[:8]}...{self.span_id[:4]}"


class DynamicConfigManager:
    """
    Hot-reloadable configuration with adaptive parameters.
    
    Config sources (priority order):
    1. Environment variables (hot-reload via signal)
    2. Defaults (hardcoded fallback)
    
    Adaptive parameters:
    - Timeouts adjust based on P95 latencies
    - Thresholds adjust based on success rates
    
    All hardcoded values are replaced with config lookups.
    """
    
    _instance: Optional["DynamicConfigManager"] = None
    _instance_lock = asyncio.Lock() if asyncio.get_event_loop_policy() else None
    
    def __init__(self):
        self._config: Dict[str, Any] = {}
        self._latency_samples: List[float] = []
        self._success_count = 0
        self._failure_count = 0
        self._last_reload = time.time()
        self._load_from_env()
    
    @classmethod
    def get_instance(cls) -> "DynamicConfigManager":
        """Get singleton instance (thread-safe in sync context)."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def _load_from_env(self):
        """Load configuration from environment variables."""
        self._config = {
            # RAG Configuration
            "rag_search_timeout": float(os.getenv("RAG_SEARCH_TIMEOUT", "5.0")),
            "rag_max_results": int(os.getenv("RAG_MAX_RESULTS", "5")),
            "rag_min_similarity": float(os.getenv("RAG_MIN_SIMILARITY", "0.3")),
            "rag_cache_ttl_seconds": float(os.getenv("RAG_CACHE_TTL_SECONDS", "30.0")),
            "rag_max_context_tokens": int(os.getenv("RAG_MAX_CONTEXT_TOKENS", "1500")),
            
            # Circuit Breaker Configuration
            "circuit_failure_threshold": int(os.getenv("CIRCUIT_FAILURE_THRESHOLD", "3")),
            "circuit_recovery_timeout": float(os.getenv("CIRCUIT_RECOVERY_TIMEOUT", "30.0")),
            "circuit_success_threshold": int(os.getenv("CIRCUIT_SUCCESS_THRESHOLD", "2")),
            
            # Async Lock Configuration
            "async_lock_timeout": float(os.getenv("ASYNC_LOCK_TIMEOUT", "5.0")),
            
            # Trinity Configuration
            "trinity_enabled": os.getenv("TRINITY_RAG_ENABLED", "true").lower() == "true",
            "trinity_search_timeout": float(os.getenv("TRINITY_SEARCH_TIMEOUT", "5.0")),
            "trinity_max_results": int(os.getenv("TRINITY_MAX_RESULTS", "5")),
            "trinity_min_similarity": float(os.getenv("TRINITY_MIN_SIMILARITY", "0.3")),
            
            # Adaptive Parameters (can be overridden)
            "adaptive_timeout_enabled": os.getenv("ADAPTIVE_TIMEOUT_ENABLED", "true").lower() == "true",
            "adaptive_p95_multiplier": float(os.getenv("ADAPTIVE_P95_MULTIPLIER", "1.5")),
        }
        self._last_reload = time.time()
        logger.debug(f"[DynamicConfig] Loaded {len(self._config)} config values")
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value."""
        return self._config.get(key, default)
    
    def record_latency(self, latency_seconds: float):
        """Record latency sample for adaptive timeout calculation."""
        self._latency_samples.append(latency_seconds)
        # Keep last 100 samples
        if len(self._latency_samples) > 100:
            self._latency_samples = self._latency_samples[-100:]
    
    def record_result(self, success: bool):
        """Record success/failure for adaptive threshold calculation."""
        if success:
            self._success_count += 1
        else:
            self._failure_count += 1
    
    def get_adaptive_timeout(self, base_key: str = "rag_search_timeout") -> float:
        """
        Get adaptive timeout based on P95 latency.
        
        If we have enough samples, use P95 * multiplier.
        Otherwise, use configured base timeout.
        """
        base_timeout = self.get(base_key, 5.0)
        
        if not self.get("adaptive_timeout_enabled", True):
            return base_timeout
        
        if len(self._latency_samples) < 10:
            return base_timeout
        
        # Calculate P95
        sorted_samples = sorted(self._latency_samples)
        p95_index = int(len(sorted_samples) * 0.95)
        p95_latency = sorted_samples[p95_index]
        
        multiplier = self.get("adaptive_p95_multiplier", 1.5)
        adaptive_timeout = p95_latency * multiplier
        
        # Clamp between 1s and 30s
        return max(1.0, min(30.0, adaptive_timeout))
    
    def get_adaptive_threshold(self, base_key: str = "rag_min_similarity") -> float:
        """
        Get adaptive similarity threshold based on success rate.
        
        If success rate is low, lower the threshold to get more results.
        """
        base_threshold = self.get(base_key, 0.3)
        
        total = self._success_count + self._failure_count
        if total < 20:
            return base_threshold
        
        success_rate = self._success_count / total
        
        # If success rate < 50%, lower threshold by up to 0.1
        if success_rate < 0.5:
            adjustment = (0.5 - success_rate) * 0.2
            return max(0.1, base_threshold - adjustment)
        
        return base_threshold
    
    def get_status(self) -> Dict[str, Any]:
        """Get config manager status."""
        return {
            "config_count": len(self._config),
            "latency_samples": len(self._latency_samples),
            "success_rate": self._success_count / max(1, self._success_count + self._failure_count),
            "last_reload": self._last_reload,
            "adaptive_timeout": self.get_adaptive_timeout(),
            "adaptive_threshold": self.get_adaptive_threshold(),
        }


# =============================================================================
# Phase 1: Unified RAG Context Manager - Global Singleton for All LLMs
# =============================================================================

class UnifiedRAGContextManager:
    """
    Global RAG context manager for all LLM pipelines.
    
    The "Brain Bridge" that provides unified RAG context to ALL LLMs:
    - Claude API via HybridOrchestrator
    - LLaMA 70B via local inference
    - Any future LLM integrations
    
    Features:
    - **Thread-safe singleton** with async lock
    - **Context caching** with TTL (avoids duplicate retrievals)
    - **Parallel retrieval** from Trinity + local KB + conversation history
    - **Correlation ID propagation** for distributed tracing
    - **Dynamic timeout** based on load
    - **Graceful degradation** when sources fail
    
    Architecture:
        ┌─────────────────────────────────────────────────────────────────┐
        │           UnifiedRAGContextManager (Singleton)                   │
        │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐   │
        │  │ Context Cache│  │ Async Lock   │  │ Correlation Context  │   │
        │  │ (TTL-based)  │  │ (Thread-safe)│  │ (Distributed Trace)  │   │
        │  └──────────────┘  └──────────────┘  └──────────────────────┘   │
        │           │                                                      │
        │           ▼                                                      │
        │  ┌─────────────────────────────────────────────────────────┐    │
        │  │                 Parallel Retrieval                       │    │
        │  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐  │    │
        │  │  │ Trinity     │  │ Local KB    │  │ Conversation    │  │    │
        │  │  │ (Web Scrape)│  │ (FAISS)     │  │ History         │  │    │
        │  │  └─────────────┘  └─────────────┘  └─────────────────┘  │    │
        │  └─────────────────────────────────────────────────────────┘    │
        │           │                                                      │
        │           ▼                                                      │
        │  ┌─────────────────────────────────────────────────────────┐    │
        │  │  Merged Context: [Web Sources] + [Local KB] + [History] │    │
        │  └─────────────────────────────────────────────────────────┘    │
        └─────────────────────────────────────────────────────────────────┘
    """
    
    _instance: Optional["UnifiedRAGContextManager"] = None
    _instance_lock: Optional[AsyncLockWithTimeout] = None
    
    def __init__(self):
        self._config = DynamicConfigManager.get_instance()
        self._rag_engine: Optional[RAGEngine] = None
        self._init_lock = AsyncLockWithTimeout(name="rag_init")
        
        # Context cache: query_hash -> (context, timestamp)
        self._context_cache: Dict[str, Tuple[Dict[str, Any], float]] = {}
        self._cache_lock = AsyncLockWithTimeout(name="cache_lock")
        
        # Metrics
        self._cache_hits = 0
        self._cache_misses = 0
        self._total_retrievals = 0
        self._failed_retrievals = 0
        
        logger.info("[UnifiedRAGContextManager] Initialized")
    
    @classmethod
    async def get_instance(cls) -> "UnifiedRAGContextManager":
        """
        Get singleton instance with async-safe initialization.
        
        Uses double-check locking pattern to avoid race conditions.
        """
        if cls._instance is not None:
            return cls._instance
        
        # Create lock if needed
        if cls._instance_lock is None:
            cls._instance_lock = AsyncLockWithTimeout(name="rag_singleton")
        
        async with cls._instance_lock.acquire_with_context(correlation_id="singleton_init"):
            # Double-check after acquiring lock
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance
    
    async def _get_rag_engine(self) -> RAGEngine:
        """Lazy-initialize RAG engine with lock protection."""
        if self._rag_engine is not None:
            return self._rag_engine
        
        async with self._init_lock.acquire_with_context(correlation_id="rag_engine_init"):
            if self._rag_engine is None:
                self._rag_engine = RAGEngine()
                logger.info("[UnifiedRAGContextManager] RAGEngine initialized")
            return self._rag_engine
    
    def _get_cache_key(self, query: str) -> str:
        """Generate cache key from query."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()
    
    async def get_context(
        self,
        query: str,
        correlation_context: Optional[CorrelationContext] = None,
        include_web_sources: bool = True,
        include_local_kb: bool = True,
        include_conversation: bool = False,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Get unified RAG context for any LLM.
        
        This is the main entry point for all RAG context retrieval.
        
        Args:
            query: User query
            correlation_context: Optional tracing context
            include_web_sources: Include Trinity web scrape results
            include_local_kb: Include local knowledge base results
            include_conversation: Include conversation history context
            conversation_history: Past conversation messages
            max_tokens: Max context tokens (default from config)
            
        Returns:
            Dict with:
                - context_text: Formatted context string for LLM
                - sources: List of source objects with metadata
                - cache_hit: Whether result was from cache
                - retrieval_time_ms: Time taken for retrieval
                - correlation_id: Trace ID for this request
        """
        start_time = time.time()
        correlation = correlation_context or CorrelationContext.create()
        cache_key = self._get_cache_key(query)
        
        # Check cache first
        cache_ttl = self._config.get("rag_cache_ttl_seconds", 30.0)
        cached = self._context_cache.get(cache_key)
        if cached:
            context, timestamp = cached
            if time.time() - timestamp < cache_ttl:
                self._cache_hits += 1
                logger.debug(f"[UnifiedRAG] Cache hit for query (correlation={correlation.short_id})")
                return {
                    **context,
                    "cache_hit": True,
                    "retrieval_time_ms": (time.time() - start_time) * 1000,
                }
        
        self._cache_misses += 1
        self._total_retrievals += 1
        
        try:
            # Parallel retrieval from multiple sources
            rag_engine = await self._get_rag_engine()
            
            tasks = []
            source_names = []
            
            # Task 1: RAG engine retrieval (includes Trinity + local KB)
            async def retrieve_rag():
                return await rag_engine.generate_with_retrieval(
                    query=query,
                    conversation_history=conversation_history,
                    include_web_sources=include_web_sources,
                )
            tasks.append(retrieve_rag())
            source_names.append("rag_engine")
            
            # Execute in parallel with timeout
            timeout = self._config.get_adaptive_timeout("rag_search_timeout")
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout,
            )
            
            # Process results
            context_text = ""
            sources = []
            web_sources = []
            
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.warning(f"[UnifiedRAG] Source '{source_names[i]}' failed: {result}")
                    continue
                
                if source_names[i] == "rag_engine" and isinstance(result, dict):
                    context_text = result.get("context_used", "")
                    sources.extend(result.get("sources", []))
                    web_sources.extend(result.get("web_sources", []))
            
            # Build final context
            max_ctx_tokens = max_tokens or self._config.get("rag_max_context_tokens", 1500)
            final_context = self._build_final_context(
                context_text, sources, web_sources, max_ctx_tokens
            )
            
            # Record latency for adaptive timeout
            latency = time.time() - start_time
            self._config.record_latency(latency)
            self._config.record_result(success=True)
            
            # Build response
            response = {
                "context_text": final_context,
                "sources": sources,
                "web_sources": web_sources,
                "cache_hit": False,
                "retrieval_time_ms": latency * 1000,
                "correlation_id": correlation.trace_id,
                "source_count": len(sources) + len(web_sources),
            }
            
            # Update cache
            async with self._cache_lock.acquire_with_context(correlation_id=correlation.short_id):
                self._context_cache[cache_key] = (response, time.time())
            
            logger.debug(
                f"[UnifiedRAG] Retrieved {len(sources)+len(web_sources)} sources "
                f"in {latency*1000:.1f}ms (correlation={correlation.short_id})"
            )
            
            return response
            
        except asyncio.TimeoutError:
            self._failed_retrievals += 1
            self._config.record_result(success=False)
            logger.warning(f"[UnifiedRAG] Retrieval timeout (correlation={correlation.short_id})")
            return {
                "context_text": "",
                "sources": [],
                "web_sources": [],
                "cache_hit": False,
                "retrieval_time_ms": (time.time() - start_time) * 1000,
                "correlation_id": correlation.trace_id,
                "error": "retrieval_timeout",
            }
        except Exception as e:
            self._failed_retrievals += 1
            self._config.record_result(success=False)
            logger.error(f"[UnifiedRAG] Retrieval failed: {e} (correlation={correlation.short_id})")
            return {
                "context_text": "",
                "sources": [],
                "web_sources": [],
                "cache_hit": False,
                "retrieval_time_ms": (time.time() - start_time) * 1000,
                "correlation_id": correlation.trace_id,
                "error": str(e),
            }
    
    def _build_final_context(
        self,
        raw_context: str,
        sources: List[Dict],
        web_sources: List[Dict],
        max_tokens: int,
    ) -> str:
        """Build final formatted context within token limit."""
        parts = []
        
        # Add web sources first (most recent/relevant)
        if web_sources:
            parts.append("=== Recent Web Knowledge ===")
            for ws in web_sources[:3]:  # Top 3
                formatted = ws.get("formatted_source", ws.get("text", ""))
                if formatted:
                    parts.append(formatted)
        
        # Add local KB context
        if raw_context and raw_context.strip():
            if parts:
                parts.append("\n=== Local Knowledge Base ===")
            parts.append(raw_context.strip())
        
        combined = "\n\n".join(parts)
        
        # Simple token limit (rough: 4 chars per token)
        max_chars = max_tokens * 4
        if len(combined) > max_chars:
            combined = combined[:max_chars] + "..."
        
        return combined
    
    def get_status(self) -> Dict[str, Any]:
        """Get manager status for monitoring."""
        return {
            "cache_size": len(self._context_cache),
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "cache_hit_rate": self._cache_hits / max(1, self._cache_hits + self._cache_misses),
            "total_retrievals": self._total_retrievals,
            "failed_retrievals": self._failed_retrievals,
            "success_rate": (self._total_retrievals - self._failed_retrievals) / max(1, self._total_retrievals),
            "config_status": self._config.get_status(),
        }
    
    async def clear_cache(self):
        """Clear the context cache."""
        async with self._cache_lock.acquire_with_context(correlation_id="cache_clear"):
            self._context_cache.clear()
            logger.info("[UnifiedRAG] Cache cleared")


# =============================================================================
# Global accessor function for UnifiedRAGContextManager
# =============================================================================

async def get_unified_rag_context(
    query: str,
    correlation_context: Optional[CorrelationContext] = None,
    **kwargs,
) -> Dict[str, Any]:
    """
    Convenience function to get unified RAG context.
    
    This is the recommended entry point for all components needing RAG context.
    
    Args:
        query: User query
        correlation_context: Optional tracing context
        **kwargs: Additional arguments passed to get_context()
        
    Returns:
        Dict with context_text, sources, and metadata
    """
    manager = await UnifiedRAGContextManager.get_instance()
    return await manager.get_context(query, correlation_context, **kwargs)


# =============================================================================
# Phase 2: ML-Enhanced Circuit Breaker with Failure Prediction
# =============================================================================

class CircuitState(Enum):
    """Circuit breaker states with graduated recovery."""
    CLOSED = "closed"          # Normal operation
    OPEN = "open"              # Failing, reject requests
    HALF_OPEN = "half_open"    # Testing recovery
    DEGRADED = "degraded"      # Partial functionality


@dataclass
class CircuitMetrics:
    """
    Enhanced metrics for ML-based failure prediction with EWMA.
    
    EWMA (Exponentially Weighted Moving Average) provides:
    - More weight to recent observations
    - Smooth trend detection
    - Seasonal pattern learning
    """
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    latency_samples: List[float] = field(default_factory=list)
    failure_timestamps: List[float] = field(default_factory=list)
    state_changes: List[Tuple[str, float]] = field(default_factory=list)
    
    # EWMA state (alpha = 0.2 for fast adaptation)
    _ewma_failure_rate: float = 0.0
    _ewma_latency: float = 0.0
    _ewma_alpha: float = 0.2
    
    # Trend detection (second-order EWMA)
    _ewma_failure_trend: float = 0.0
    _ewma_latency_trend: float = 0.0
    
    # Hourly patterns for time-of-day awareness
    _hourly_failure_rates: Dict[int, List[float]] = field(default_factory=lambda: {h: [] for h in range(24)})
    
    def update_ewma(self, success: bool, latency: float):
        """Update EWMA metrics with new observation."""
        # Update failure rate EWMA
        failure_value = 0.0 if success else 1.0
        old_ewma = self._ewma_failure_rate
        self._ewma_failure_rate = (
            self._ewma_alpha * failure_value +
            (1 - self._ewma_alpha) * self._ewma_failure_rate
        )
        
        # Track trend (derivative of EWMA)
        self._ewma_failure_trend = (
            self._ewma_alpha * (self._ewma_failure_rate - old_ewma) +
            (1 - self._ewma_alpha) * self._ewma_failure_trend
        )
        
        # Update latency EWMA
        old_latency_ewma = self._ewma_latency
        self._ewma_latency = (
            self._ewma_alpha * latency +
            (1 - self._ewma_alpha) * self._ewma_latency
        )
        
        # Latency trend
        self._ewma_latency_trend = (
            self._ewma_alpha * (self._ewma_latency - old_latency_ewma) +
            (1 - self._ewma_alpha) * self._ewma_latency_trend
        )
        
        # Update hourly pattern
        hour = int(time.time() // 3600) % 24
        if hour not in self._hourly_failure_rates:
            self._hourly_failure_rates[hour] = []
        self._hourly_failure_rates[hour].append(failure_value)
        # Keep last 100 per hour
        if len(self._hourly_failure_rates[hour]) > 100:
            self._hourly_failure_rates[hour] = self._hourly_failure_rates[hour][-100:]
    
    @property
    def predicted_failure_rate(self) -> float:
        """Predict future failure rate using EWMA + trend."""
        # Base prediction from EWMA
        prediction = self._ewma_failure_rate
        
        # Adjust for trend (if failures increasing, predict higher)
        if self._ewma_failure_trend > 0:
            prediction += self._ewma_failure_trend * 5  # Project 5 steps ahead
        
        # Adjust for time-of-day pattern
        hour = int(time.time() // 3600) % 24
        hourly_rates = self._hourly_failure_rates.get(hour, [])
        if len(hourly_rates) >= 10:
            hourly_avg = sum(hourly_rates[-10:]) / 10
            # Weight hourly pattern 20%
            prediction = 0.8 * prediction + 0.2 * hourly_avg
        
        return min(1.0, max(0.0, prediction))
    
    @property
    def is_degrading(self) -> bool:
        """Check if system is trending toward failure."""
        return self._ewma_failure_trend > 0.01 or self._ewma_latency_trend > 0.1
    
    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 1.0
        return self.successful_calls / self.total_calls
    
    @property
    def failure_rate(self) -> float:
        return 1.0 - self.success_rate
    
    @property
    def p50_latency(self) -> float:
        if len(self.latency_samples) < 5:
            return 0.0
        sorted_samples = sorted(self.latency_samples)
        return sorted_samples[len(sorted_samples) // 2]
    
    @property
    def p95_latency(self) -> float:
        if len(self.latency_samples) < 10:
            return 0.0
        sorted_samples = sorted(self.latency_samples)
        return sorted_samples[int(len(sorted_samples) * 0.95)]
    
    @property
    def p99_latency(self) -> float:
        if len(self.latency_samples) < 20:
            return 0.0
        sorted_samples = sorted(self.latency_samples)
        return sorted_samples[int(len(sorted_samples) * 0.99)]


class AdaptiveMLCircuitBreaker:
    """
    ML-enhanced circuit breaker with failure prediction.
    
    Features:
    - **Exponential smoothing** for failure rate prediction
    - **Adaptive thresholds** based on rolling success rate
    - **Jitter in recovery** to prevent thundering herd
    - **Half-open with graduated traffic** (10% → 50% → 100%)
    - **Degraded mode** for partial functionality
    
    Algorithm:
    1. Track failure timestamps with exponential decay
    2. Predict failure probability in next N seconds
    3. Pre-emptively open circuit if P(failure) > threshold
    4. Gradual recovery with random jitter
    """
    
    def __init__(
        self,
        name: str,
        failure_threshold: Optional[int] = None,
        recovery_timeout: Optional[float] = None,
        success_threshold: Optional[int] = None,
        prediction_enabled: bool = True,
    ):
        self._name = name
        self._config = DynamicConfigManager.get_instance()
        
        # Use config or defaults
        self._failure_threshold = failure_threshold or self._config.get("circuit_failure_threshold", 3)
        self._recovery_timeout = recovery_timeout or self._config.get("circuit_recovery_timeout", 30.0)
        self._success_threshold = success_threshold or self._config.get("circuit_success_threshold", 2)
        
        # State
        self._state = CircuitState.CLOSED
        self._last_failure_time: Optional[float] = None
        self._last_state_change = time.time()
        self._half_open_successes = 0
        self._half_open_traffic_pct = 0.1  # Start at 10%
        
        # Metrics
        self._metrics = CircuitMetrics()
        
        # ML prediction
        self._prediction_enabled = prediction_enabled
        self._smoothing_factor = 0.3  # Exponential smoothing alpha
        self._predicted_failure_rate = 0.0
        self._prediction_threshold = 0.7  # Open if P(failure) > 70%
        
        logger.info(f"[AdaptiveMLCircuitBreaker:{name}] Initialized")
    
    @property
    def state(self) -> CircuitState:
        return self._state
    
    @property
    def is_available(self) -> bool:
        """Check if circuit allows requests."""
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.DEGRADED:
            return True  # Allow with degraded functionality
        if self._state == CircuitState.HALF_OPEN:
            # Probabilistic allow based on graduated traffic
            return random.random() < self._half_open_traffic_pct
        if self._state == CircuitState.OPEN:
            return self._should_attempt_recovery()
        return False
    
    def _should_attempt_recovery(self) -> bool:
        """Check if we should transition to half-open."""
        if self._last_failure_time is None:
            return True
        
        elapsed = time.time() - self._last_failure_time
        # Add jitter to prevent thundering herd
        jitter = random.uniform(0, self._recovery_timeout * 0.2)
        
        if elapsed > self._recovery_timeout + jitter:
            self._transition_to(CircuitState.HALF_OPEN)
            return True
        return False
    
    def _transition_to(self, new_state: CircuitState):
        """Transition to new state with logging."""
        if new_state != self._state:
            old_state = self._state
            self._state = new_state
            self._last_state_change = time.time()
            self._metrics.state_changes.append((new_state.value, time.time()))
            
            if new_state == CircuitState.HALF_OPEN:
                self._half_open_successes = 0
                self._half_open_traffic_pct = 0.1  # Reset to 10%
            
            logger.info(f"[AdaptiveMLCircuitBreaker:{self._name}] {old_state.value} → {new_state.value}")
    
    def _update_prediction(self):
        """Update failure rate prediction using exponential smoothing."""
        if not self._prediction_enabled:
            return
        
        # Calculate recent failure rate (last 60s)
        now = time.time()
        recent_failures = [t for t in self._metrics.failure_timestamps if now - t < 60]
        recent_rate = len(recent_failures) / max(1, self._metrics.total_calls)
        
        # Exponential smoothing
        self._predicted_failure_rate = (
            self._smoothing_factor * recent_rate +
            (1 - self._smoothing_factor) * self._predicted_failure_rate
        )
        
        # Pre-emptive circuit open
        if (self._state == CircuitState.CLOSED and 
            self._predicted_failure_rate > self._prediction_threshold):
            logger.warning(
                f"[AdaptiveMLCircuitBreaker:{self._name}] Pre-emptive OPEN "
                f"(predicted failure rate: {self._predicted_failure_rate:.2%})"
            )
            self._transition_to(CircuitState.DEGRADED)  # Go to degraded, not full open
    
    async def execute(
        self,
        func: Callable,
        *args,
        fallback: Optional[Callable] = None,
        **kwargs,
    ) -> Any:
        """
        Execute function with circuit breaker protection.
        
        Args:
            func: Async function to execute
            fallback: Optional fallback function if circuit is open
            *args, **kwargs: Arguments for func
            
        Returns:
            Function result or fallback result
            
        Raises:
            CircuitOpenError: If circuit is open and no fallback
        """
        if not self.is_available:
            if fallback:
                logger.debug(f"[AdaptiveMLCircuitBreaker:{self._name}] Using fallback")
                return await fallback(*args, **kwargs) if asyncio.iscoroutinefunction(fallback) else fallback(*args, **kwargs)
            raise Exception(f"Circuit breaker '{self._name}' is OPEN")
        
        start_time = time.time()
        try:
            result = await func(*args, **kwargs)
            latency = time.time() - start_time
            self._on_success(latency)
            return result
        except Exception as e:
            self._on_failure()
            raise
    
    def _on_success(self, latency: float):
        """Record successful call with EWMA update."""
        self._metrics.total_calls += 1
        self._metrics.successful_calls += 1
        self._metrics.consecutive_successes += 1
        self._metrics.consecutive_failures = 0
        self._metrics.latency_samples.append(latency)
        
        # Update EWMA metrics
        self._metrics.update_ewma(success=True, latency=latency)
        
        # Keep last 100 samples
        if len(self._metrics.latency_samples) > 100:
            self._metrics.latency_samples = self._metrics.latency_samples[-100:]
        
        # State transitions
        if self._state == CircuitState.HALF_OPEN:
            self._half_open_successes += 1
            
            # Graduated recovery with exponential ramp
            if self._half_open_successes >= 2:
                # Exponential: 10% → 20% → 40% → 80% → 100%
                self._half_open_traffic_pct = min(1.0, self._half_open_traffic_pct * 2)
                
            if self._half_open_successes >= self._success_threshold:
                self._transition_to(CircuitState.CLOSED)
        
        elif self._state == CircuitState.DEGRADED:
            # Check trend — only recover if not degrading
            if not self._metrics.is_degrading:
                if self._metrics.consecutive_successes >= self._success_threshold:
                    self._transition_to(CircuitState.CLOSED)
        
        self._update_prediction()
    
    def _on_failure(self):
        """Record failed call with EWMA update and early degradation."""
        self._metrics.total_calls += 1
        self._metrics.failed_calls += 1
        self._metrics.consecutive_failures += 1
        self._metrics.consecutive_successes = 0
        self._metrics.failure_timestamps.append(time.time())
        self._last_failure_time = time.time()
        
        # Update EWMA metrics (record failure with worst-case latency estimate)
        self._metrics.update_ewma(success=False, latency=self._recovery_timeout)
        
        # Keep last 100 timestamps
        if len(self._metrics.failure_timestamps) > 100:
            self._metrics.failure_timestamps = self._metrics.failure_timestamps[-100:]
        
        # State transitions with early degradation
        if self._state == CircuitState.HALF_OPEN:
            self._transition_to(CircuitState.OPEN)
            
        elif self._state == CircuitState.CLOSED:
            # Check for early degradation (trend-based)
            if self._metrics.is_degrading and self._metrics.consecutive_failures >= 2:
                logger.warning(
                    f"[AdaptiveMLCircuitBreaker:{self._name}] Early DEGRADED "
                    f"(trend indicates increasing failures)"
                )
                self._transition_to(CircuitState.DEGRADED)
            elif self._metrics.consecutive_failures >= self._failure_threshold:
                self._transition_to(CircuitState.OPEN)
        
        self._update_prediction()
    
    def get_status(self) -> Dict[str, Any]:
        """Get circuit breaker status."""
        return {
            "name": self._name,
            "state": self._state.value,
            "success_rate": self._metrics.success_rate,
            "failure_rate": self._metrics.failure_rate,
            "predicted_failure_rate": self._predicted_failure_rate,
            "consecutive_failures": self._metrics.consecutive_failures,
            "p95_latency": self._metrics.p95_latency,
            "total_calls": self._metrics.total_calls,
            "half_open_traffic_pct": self._half_open_traffic_pct if self._state == CircuitState.HALF_OPEN else None,
        }


# =============================================================================
# Phase 2: Unified Resilience Layer with Backpressure & Graceful Degradation
# =============================================================================

class TokenBucket:
    """Token bucket rate limiter for backpressure."""
    
    def __init__(
        self,
        rate: float,  # tokens per second
        capacity: int,  # max burst
    ):
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._last_update = time.time()
        self._lock = asyncio.Lock()
    
    async def acquire(self, tokens: int = 1, timeout: float = 5.0) -> bool:
        """Try to acquire tokens, waiting if necessary."""
        async with self._lock:
            self._refill()
            
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            
            # Wait for tokens
            needed = tokens - self._tokens
            wait_time = needed / self._rate
            
            if wait_time > timeout:
                return False
            
            await asyncio.sleep(wait_time)
            self._refill()
            
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False
    
    def _refill(self):
        """Refill tokens based on elapsed time."""
        now = time.time()
        elapsed = now - self._last_update
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_update = now
    
    @property
    def available_tokens(self) -> float:
        """Get current available tokens."""
        self._refill()
        return self._tokens


class RequestDeduplicator:
    """
    Enhanced deduplicator with semantic matching and result caching.
    
    Features:
    - **In-flight deduplication**: Concurrent identical requests share result
    - **Semantic matching**: Normalizes queries (case, whitespace, punctuation)
    - **Result cache**: Stores recent results for fast replay
    - **Similar query detection**: Hash prefix matching for near-duplicates
    """
    
    def __init__(
        self,
        ttl_seconds: float = 30.0,
        cache_ttl_seconds: float = 60.0,
        similarity_threshold: float = 0.9,
    ):
        self._in_flight: Dict[str, asyncio.Future] = {}
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()
        
        # Result cache: normalized_key -> (result, timestamp)
        self._result_cache: Dict[str, Tuple[Any, float]] = {}
        self._cache_ttl = cache_ttl_seconds
        self._cache_hits = 0
        self._cache_misses = 0
        
        # Semantic matching patterns
        self._normalize_patterns = [
            (re.compile(r'\s+'), ' '),           # Collapse whitespace
            (re.compile(r'[^\w\s]'), ''),         # Remove punctuation
            (re.compile(r'\b(what|tell|show|me|about|the|a|an|is)\b', re.I), ''),  # Stop words
        ]
    
    def _normalize_query(self, query: str) -> str:
        """Normalize query for semantic matching."""
        normalized = query.lower().strip()
        for pattern, replacement in self._normalize_patterns:
            normalized = pattern.sub(replacement, normalized)
        return ' '.join(normalized.split())  # Clean up extra spaces
    
    def _get_cache_key(self, query: str) -> str:
        """Generate cache key from normalized query."""
        normalized = self._normalize_query(query)
        return hashlib.md5(normalized.encode()).hexdigest()
    
    def _check_cache(self, key: str) -> Optional[Any]:
        """Check if we have a cached result."""
        if key in self._result_cache:
            result, timestamp = self._result_cache[key]
            if time.time() - timestamp < self._cache_ttl:
                self._cache_hits += 1
                return result
            # Expired, remove
            del self._result_cache[key]
        self._cache_misses += 1
        return None
    
    def _update_cache(self, key: str, result: Any):
        """Update result cache."""
        self._result_cache[key] = (result, time.time())
        
        # Cleanup old entries (keep max 500)
        if len(self._result_cache) > 500:
            # Remove oldest entries
            sorted_keys = sorted(
                self._result_cache.keys(),
                key=lambda k: self._result_cache[k][1]
            )
            for old_key in sorted_keys[:100]:
                del self._result_cache[old_key]
    
    async def dedupe(
        self,
        key: str,
        func: Callable,
        *args,
        use_cache: bool = True,
        **kwargs,
    ) -> Any:
        """
        Execute function with deduplication and caching.
        
        Args:
            key: Original query or request key
            func: Async function to execute
            use_cache: Whether to check/update result cache
            *args, **kwargs: Function arguments
            
        Returns:
            Function result (possibly from cache or in-flight request)
        """
        # Normalize key for semantic matching
        cache_key = self._get_cache_key(key)
        
        # Check cache first
        if use_cache:
            cached = self._check_cache(cache_key)
            if cached is not None:
                logger.debug(f"[Dedup] Cache hit for query: {key[:30]}...")
                return cached
        
        async with self._lock:
            # Check if in-flight (use cache key for semantic matching)
            if cache_key in self._in_flight:
                logger.debug(f"[Dedup] Waiting for in-flight request: {key[:20]}...")
                return await self._in_flight[cache_key]
            
            # Create future for this request
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            self._in_flight[cache_key] = future
        
        try:
            result = await func(*args, **kwargs)
            future.set_result(result)
            
            # Update cache
            if use_cache:
                self._update_cache(cache_key, result)
            
            return result
        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            async with self._lock:
                self._in_flight.pop(cache_key, None)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get deduplicator statistics."""
        total = self._cache_hits + self._cache_misses
        return {
            "cache_size": len(self._result_cache),
            "in_flight_count": len(self._in_flight),
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "cache_hit_rate": self._cache_hits / max(1, total),
        }


class PartialResultAggregator:
    """
    Aggregates partial results from multiple sources for graceful degradation.
    
    When some sources fail (Trinity, local KB, etc.), this aggregator:
    - Collects successful results
    - Tracks failed sources with reasons
    - Provides quality score (0.0 = no results, 1.0 = all sources succeeded)
    - Enables informed degradation decisions
    
    Usage:
        aggregator = PartialResultAggregator(sources=["trinity", "local_kb", "cache"])
        aggregator.add_result("trinity", web_results)
        aggregator.add_failure("local_kb", "Timeout")
        aggregator.add_result("cache", cached_results)
        
        final = aggregator.get_aggregated()
        # {
        #     "context_text": "...",
        #     "sources": [...],
        #     "quality_score": 0.67,
        #     "degraded": True,
        #     "failed_sources": ["local_kb"]
        # }
    """
    
    def __init__(self, sources: List[str]):
        self._expected_sources = sources
        self._results: Dict[str, Any] = {}
        self._failures: Dict[str, str] = {}
        self._start_time = time.time()
    
    def add_result(self, source: str, result: Any):
        """Add successful result from a source."""
        self._results[source] = result
    
    def add_failure(self, source: str, reason: str):
        """Record a failed source with reason."""
        self._failures[source] = reason
    
    @property
    def quality_score(self) -> float:
        """Quality score: ratio of successful sources."""
        if not self._expected_sources:
            return 1.0
        return len(self._results) / len(self._expected_sources)
    
    @property
    def is_degraded(self) -> bool:
        """True if any source failed."""
        return len(self._failures) > 0
    
    @property
    def failed_sources(self) -> List[str]:
        """List of failed sources."""
        return list(self._failures.keys())
    
    def get_aggregated(self) -> Dict[str, Any]:
        """
        Get aggregated result with quality metadata.
        
        Returns:
            Dict with:
            - context_text: Combined context from all successful sources
            - sources: List of all sources that contributed
            - web_sources: List of web sources (from Trinity)
            - quality_score: 0.0-1.0 indicating result completeness
            - degraded: True if any source failed
            - failed_sources: List of failed source names
            - latency_ms: Time taken to aggregate
        """
        context_parts = []
        all_sources = []
        web_sources = []
        
        # Process results by priority (Trinity first for recency)
        for source in ["trinity", "local_kb", "cache"]:
            if source in self._results:
                result = self._results[source]
                
                if isinstance(result, dict):
                    # Dict result (typical RAG response)
                    if result.get("context_text"):
                        context_parts.append(result["context_text"])
                    if result.get("sources"):
                        all_sources.extend(result["sources"])
                    if result.get("web_sources"):
                        web_sources.extend(result["web_sources"])
                elif isinstance(result, list):
                    # List of results (search results)
                    for item in result:
                        if isinstance(item, dict):
                            text = item.get("text", item.get("content", ""))
                            if text:
                                context_parts.append(text)
                            all_sources.append(item)
                elif isinstance(result, str):
                    # String result (raw context)
                    context_parts.append(result)
        
        return {
            "context_text": "\n\n---\n\n".join(context_parts) if context_parts else "",
            "sources": all_sources,
            "web_sources": web_sources,
            "quality_score": self.quality_score,
            "degraded": self.is_degraded,
            "failed_sources": self.failed_sources,
            "failure_reasons": self._failures,
            "latency_ms": (time.time() - self._start_time) * 1000,
        }


class UnifiedResilienceLayer:
    """
    Central resilience coordinator for all RAG operations.
    
    Features:
    - **Backpressure**: Token bucket rate limiting
    - **Request deduplication**: Prevent duplicate retrievals
    - **Graceful degradation**: Return partial results on failure
    - **Load shedding**: Drop low-priority requests under load
    - **Circuit breakers**: Per-source failure isolation
    
    Architecture:
        ┌─────────────────────────────────────────────────────────────────┐
        │             UnifiedResilienceLayer (Singleton)                   │
        │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐   │
        │  │ Token Bucket │  │ Deduplicator │  │ Circuit Breakers     │   │
        │  │ (Backpressure│  │              │  │ (Per-Source)         │   │
        │  └──────────────┘  └──────────────┘  └──────────────────────┘   │
        │           │                                                      │
        │           ▼                                                      │
        │  ┌─────────────────────────────────────────────────────────┐    │
        │  │  Graceful Degradation: Partial Results on Failure       │    │
        │  └─────────────────────────────────────────────────────────┘    │
        └─────────────────────────────────────────────────────────────────┘
    """
    
    _instance: Optional["UnifiedResilienceLayer"] = None
    _instance_lock: Optional[AsyncLockWithTimeout] = None
    
    def __init__(self):
        self._config = DynamicConfigManager.get_instance()
        
        # Token bucket for backpressure (default 50 req/s, burst 100)
        rate = float(os.getenv("RESILIENCE_RATE_LIMIT", "50"))
        capacity = int(os.getenv("RESILIENCE_BURST_CAPACITY", "100"))
        self._token_bucket = TokenBucket(rate=rate, capacity=capacity)
        
        # Request deduplicator
        self._deduplicator = RequestDeduplicator(ttl_seconds=30.0)
        
        # Per-source circuit breakers
        self._circuit_breakers: Dict[str, AdaptiveMLCircuitBreaker] = {}
        
        # Load tracking
        self._current_load = 0
        self._max_load = int(os.getenv("RESILIENCE_MAX_LOAD", "100"))
        self._load_lock = asyncio.Lock()
        
        # Metrics
        self._shed_count = 0
        self._degraded_count = 0
        self._total_requests = 0
        
        logger.info("[UnifiedResilienceLayer] Initialized")
    
    @classmethod
    async def get_instance(cls) -> "UnifiedResilienceLayer":
        """Get singleton instance."""
        if cls._instance is not None:
            return cls._instance
        
        if cls._instance_lock is None:
            cls._instance_lock = AsyncLockWithTimeout(name="resilience_singleton")
        
        async with cls._instance_lock.acquire_with_context(correlation_id="resilience_init"):
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance
    
    def get_circuit_breaker(self, source: str) -> AdaptiveMLCircuitBreaker:
        """Get or create circuit breaker for a source."""
        if source not in self._circuit_breakers:
            self._circuit_breakers[source] = AdaptiveMLCircuitBreaker(name=source)
        return self._circuit_breakers[source]
    
    async def execute_with_resilience(
        self,
        source: str,
        func: Callable,
        *args,
        priority: int = 1,  # 0=low, 1=normal, 2=high
        fallback: Optional[Callable] = None,
        dedupe_key: Optional[str] = None,
        **kwargs,
    ) -> Tuple[Any, bool]:
        """
        Execute function with full resilience protection.
        
        Args:
            source: Source name for circuit breaker
            func: Async function to execute
            priority: Request priority for load shedding
            fallback: Optional fallback on failure
            dedupe_key: Optional key for deduplication
            *args, **kwargs: Function arguments
            
        Returns:
            Tuple of (result, was_degraded)
        """
        self._total_requests += 1
        
        # 1. Load shedding for low priority under high load
        async with self._load_lock:
            load_pct = self._current_load / max(1, self._max_load)
            if load_pct > 0.9 and priority == 0:
                self._shed_count += 1
                logger.warning(f"[Resilience] Shedding low-priority request (load: {load_pct:.0%})")
                if fallback:
                    return await fallback(*args, **kwargs), True
                raise Exception("Load shedding: system overloaded")
            self._current_load += 1
        
        try:
            # 2. Backpressure via token bucket
            if not await self._token_bucket.acquire(tokens=1, timeout=5.0):
                self._degraded_count += 1
                logger.warning("[Resilience] Backpressure: rate limit exceeded")
                if fallback:
                    return await fallback(*args, **kwargs), True
                raise Exception("Backpressure: rate limit exceeded")
            
            # 3. Get circuit breaker for source
            cb = self.get_circuit_breaker(source)
            
            # 4. Execute with deduplication if key provided
            async def execute():
                return await cb.execute(func, *args, fallback=fallback, **kwargs)
            
            if dedupe_key:
                result = await self._deduplicator.dedupe(dedupe_key, execute)
            else:
                result = await execute()
            
            return result, False
            
        except Exception as e:
            # Graceful degradation: try fallback
            if fallback:
                self._degraded_count += 1
                try:
                    result = await fallback(*args, **kwargs) if asyncio.iscoroutinefunction(fallback) else fallback(*args, **kwargs)
                    return result, True
                except Exception:
                    pass
            raise
        finally:
            async with self._load_lock:
                self._current_load = max(0, self._current_load - 1)
    
    def get_status(self) -> Dict[str, Any]:
        """Get resilience layer status."""
        return {
            "current_load": self._current_load,
            "max_load": self._max_load,
            "load_pct": self._current_load / max(1, self._max_load),
            "available_tokens": self._token_bucket.available_tokens,
            "total_requests": self._total_requests,
            "shed_count": self._shed_count,
            "degraded_count": self._degraded_count,
            "circuit_breakers": {
                name: cb.get_status()
                for name, cb in self._circuit_breakers.items()
            },
        }


# =============================================================================
# Phase 2: Service Registry for Cross-Repo Health Monitoring
# =============================================================================

@dataclass
class ServiceEndpoint:
    """Represents a service endpoint with health info."""
    name: str
    url: str
    health_path: str = "/health"
    is_healthy: bool = True
    last_check: float = 0.0
    consecutive_failures: int = 0
    latency_ms: float = 0.0
    tags: Dict[str, str] = field(default_factory=dict)


class ServiceRegistry:
    """
    Dynamic service discovery with health-aware routing.
    
    Services:
    - JARVIS (this instance)
    - JARVIS-Prime (inference)
    - Reactor-Core (training)
    - Trinity Indexer (embedded)
    
    Features:
    - Periodic health checks with adaptive intervals
    - Automatic failover on degradation
    - Weighted routing based on latency
    """
    
    _instance: Optional["ServiceRegistry"] = None
    
    def __init__(self):
        self._services: Dict[str, ServiceEndpoint] = {}
        self._check_interval = float(os.getenv("SERVICE_CHECK_INTERVAL", "30.0"))
        self._check_task: Optional[asyncio.Task] = None
        self._running = False
        
        # Register default services
        self._register_defaults()
        
        logger.info("[ServiceRegistry] Initialized")
    
    @classmethod
    def get_instance(cls) -> "ServiceRegistry":
        """Get singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def _register_defaults(self):
        """Register default JARVIS ecosystem services."""
        defaults = [
            ServiceEndpoint(
                name="jarvis_prime",
                url=os.getenv("JARVIS_PRIME_URL", "http://localhost:8000"),
                health_path="/health",
                tags={"type": "inference"},
            ),
            ServiceEndpoint(
                name="reactor_core",
                url=os.getenv("REACTOR_CORE_URL", "http://localhost:8090"),
                health_path="/health",
                tags={"type": "training"},
            ),
            ServiceEndpoint(
                name="trinity_indexer",
                url="embedded://localhost",
                health_path="",
                tags={"type": "indexer"},
            ),
        ]
        
        for svc in defaults:
            self._services[svc.name] = svc
    
    def register(self, endpoint: ServiceEndpoint):
        """Register a service endpoint."""
        self._services[endpoint.name] = endpoint
        logger.info(f"[ServiceRegistry] Registered: {endpoint.name} at {endpoint.url}")
    
    def get_healthy_endpoints(self, tag: Optional[str] = None) -> List[ServiceEndpoint]:
        """Get all healthy endpoints, optionally filtered by tag."""
        healthy = [svc for svc in self._services.values() if svc.is_healthy]
        if tag:
            healthy = [svc for svc in healthy if svc.tags.get("type") == tag]
        # Sort by latency (fastest first)
        return sorted(healthy, key=lambda s: s.latency_ms)
    
    async def check_health(self, service_name: str) -> bool:
        """Check health of a specific service."""
        if service_name not in self._services:
            return False
        
        svc = self._services[service_name]
        
        # Embedded services are always healthy
        if svc.url.startswith("embedded://"):
            svc.is_healthy = True
            svc.last_check = time.time()
            return True
        
        try:
            import aiohttp
            start = time.time()
            
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.get(f"{svc.url}{svc.health_path}") as resp:
                    latency = (time.time() - start) * 1000
                    
                    if resp.status == 200:
                        svc.is_healthy = True
                        svc.consecutive_failures = 0
                        svc.latency_ms = latency
                    else:
                        svc.consecutive_failures += 1
                        if svc.consecutive_failures >= 3:
                            svc.is_healthy = False
                    
                    svc.last_check = time.time()
                    return svc.is_healthy
                    
        except Exception as e:
            svc.consecutive_failures += 1
            if svc.consecutive_failures >= 3:
                svc.is_healthy = False
            svc.last_check = time.time()
            logger.warning(f"[ServiceRegistry] Health check failed for {service_name}: {e}")
            return False
    
    async def start_health_checks(self):
        """Start periodic health checks."""
        if self._running:
            return
        self._running = True
        self._check_task = asyncio.create_task(self._health_check_loop())
        logger.info("[ServiceRegistry] Started health checks")
    
    async def _health_check_loop(self):
        """Background health check loop."""
        while self._running:
            try:
                for name in list(self._services.keys()):
                    await self.check_health(name)
                await asyncio.sleep(self._check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ServiceRegistry] Health check error: {e}")
                await asyncio.sleep(5)
    
    async def stop(self):
        """Stop health checks."""
        self._running = False
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
    
    def get_status(self) -> Dict[str, Any]:
        """Get registry status."""
        return {
            "services": {
                name: {
                    "url": svc.url,
                    "is_healthy": svc.is_healthy,
                    "latency_ms": svc.latency_ms,
                    "consecutive_failures": svc.consecutive_failures,
                    "last_check": svc.last_check,
                }
                for name, svc in self._services.items()
            },
            "healthy_count": len([s for s in self._services.values() if s.is_healthy]),
            "total_count": len(self._services),
        }


# =============================================================================
# Global accessor functions for Phase 2 components
# =============================================================================

async def execute_with_resilience(
    source: str,
    func: Callable,
    *args,
    **kwargs,
) -> Tuple[Any, bool]:
    """
    Convenience function for resilient execution.
    
    Returns:
        Tuple of (result, was_degraded)
    """
    layer = await UnifiedResilienceLayer.get_instance()
    return await layer.execute_with_resilience(source, func, *args, **kwargs)


def get_service_registry() -> ServiceRegistry:
    """Get the global service registry."""
    return ServiceRegistry.get_instance()


# =============================================================================
# Phase 2.2: Cross-Repo Health Aggregation
# =============================================================================

class CrossRepoHealthAggregator:
    """
    Aggregates health status across JARVIS ecosystem (JARVIS, J-Prime, Reactor-Core).
    
    Features:
    - **Dependency graph**: Knows which services depend on which
    - **Cascading degradation**: If dependency unhealthy, mark dependents degraded
    - **Parallel health checks**: Check all services concurrently
    - **Health history**: Track health trends over time
    - **Predictive alerts**: Warn before cascade failures
    """
    
    _instance: Optional["CrossRepoHealthAggregator"] = None
    
    # Service dependency graph
    DEPENDENCIES = {
        "jarvis": [],  # JARVIS is root
        "jarvis_prime": ["jarvis"],  # J-Prime depends on JARVIS
        "reactor_core": ["jarvis", "jarvis_prime"],  # Reactor depends on both
        "trinity_indexer": ["jarvis"],  # Trinity depends on JARVIS
    }
    
    def __init__(self):
        self._registry = ServiceRegistry.get_instance()
        self._health_history: Dict[str, List[Tuple[float, bool]]] = {}
        self._aggregated_status: Dict[str, str] = {}  # healthy, degraded, unhealthy
        self._last_aggregate_time = 0.0
        self._aggregate_lock = asyncio.Lock()
        
        logger.info("[CrossRepoHealthAggregator] Initialized")
    
    @classmethod
    def get_instance(cls) -> "CrossRepoHealthAggregator":
        """Get singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    async def aggregate_health(self) -> Dict[str, Any]:
        """
        Aggregate health across all services with dependency awareness.
        
        Returns:
            Dict with per-service health, overall score, and alerts
        """
        async with self._aggregate_lock:
            now = time.time()
            
            # Get current health from registry
            registry_status = self._registry.get_status()
            services = registry_status.get("services", {})
            
            # Check each service
            for name, info in services.items():
                is_healthy = info.get("is_healthy", False)
                
                # Record history
                if name not in self._health_history:
                    self._health_history[name] = []
                self._health_history[name].append((now, is_healthy))
                
                # Keep last 100 entries
                if len(self._health_history[name]) > 100:
                    self._health_history[name] = self._health_history[name][-100:]
                
                # Determine status with dependency awareness
                if not is_healthy:
                    self._aggregated_status[name] = "unhealthy"
                else:
                    # Check if any dependency is unhealthy
                    deps = self.DEPENDENCIES.get(name, [])
                    dep_unhealthy = any(
                        self._aggregated_status.get(d) in ("unhealthy", "degraded")
                        for d in deps
                    )
                    if dep_unhealthy:
                        self._aggregated_status[name] = "degraded"
                    else:
                        self._aggregated_status[name] = "healthy"
            
            self._last_aggregate_time = now
            
            # Calculate overall system health
            statuses = list(self._aggregated_status.values())
            healthy_count = statuses.count("healthy")
            total = len(statuses) if statuses else 1
            
            return {
                "services": self._aggregated_status.copy(),
                "overall_score": healthy_count / total,
                "healthy_count": healthy_count,
                "degraded_count": statuses.count("degraded"),
                "unhealthy_count": statuses.count("unhealthy"),
                "alerts": self._generate_alerts(),
                "timestamp": now,
            }
    
    def _generate_alerts(self) -> List[str]:
        """Generate predictive alerts based on health trends."""
        alerts = []
        
        for name, history in self._health_history.items():
            if len(history) < 5:
                continue
            
            # Check recent trend
            recent = [h[1] for h in history[-10:]]
            failure_rate = recent.count(False) / len(recent)
            
            if failure_rate > 0.5:
                alerts.append(f"CRITICAL: {name} has {failure_rate:.0%} failure rate")
            elif failure_rate > 0.2:
                alerts.append(f"WARNING: {name} showing degradation ({failure_rate:.0%} failures)")
        
        return alerts
    
    def get_service_status(self, service_name: str) -> str:
        """Get current aggregated status for a service."""
        return self._aggregated_status.get(service_name, "unknown")


# =============================================================================
# Phase 2.2: Resource Bulkhead for Service Isolation
# =============================================================================

class ResourceBulkhead:
    """
    Bulkhead pattern for service isolation with semaphore-based concurrency limits.
    
    Prevents one slow service from consuming all resources.
    Each service gets its own "compartment" with limited slots.
    
    Features:
    - **Per-service isolation**: Separate limits for each service
    - **Adaptive sizing**: Grows/shrinks based on success rate
    - **Queue depth monitoring**: Track waiting requests
    - **Timeout with rejection**: Reject if wait too long
    """
    
    def __init__(
        self,
        name: str,
        max_concurrent: int = 10,
        queue_depth: int = 50,
        adaptive: bool = True,
    ):
        self._name = name
        self._max_concurrent = max_concurrent
        self._initial_max = max_concurrent
        self._queue_depth = queue_depth
        self._adaptive = adaptive
        
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active_count = 0
        self._queued_count = 0
        self._rejected_count = 0
        self._success_count = 0
        self._failure_count = 0
        self._lock = asyncio.Lock()
        
        logger.info(f"[Bulkhead:{name}] Initialized (max={max_concurrent})")
    
    @property
    def is_full(self) -> bool:
        """Check if bulkhead is at capacity."""
        return self._active_count >= self._max_concurrent
    
    @property
    def queue_full(self) -> bool:
        """Check if queue is full."""
        return self._queued_count >= self._queue_depth
    
    async def acquire(self, timeout: float = 5.0) -> bool:
        """
        Acquire a slot in the bulkhead.
        
        Returns:
            True if acquired, False if rejected
        """
        # Check queue depth
        async with self._lock:
            if self.queue_full:
                self._rejected_count += 1
                logger.warning(f"[Bulkhead:{self._name}] Rejected (queue full)")
                return False
            self._queued_count += 1
        
        try:
            # Try to acquire semaphore with timeout
            acquired = await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=timeout
            )
            
            async with self._lock:
                self._queued_count -= 1
                if acquired:
                    self._active_count += 1
                    return True
                return False
                
        except asyncio.TimeoutError:
            async with self._lock:
                self._queued_count -= 1
                self._rejected_count += 1
            logger.warning(f"[Bulkhead:{self._name}] Timeout waiting for slot")
            return False
    
    async def release(self, success: bool = True):
        """Release a slot back to the bulkhead."""
        self._semaphore.release()
        
        async with self._lock:
            self._active_count = max(0, self._active_count - 1)
            if success:
                self._success_count += 1
            else:
                self._failure_count += 1
            
            # Adaptive sizing
            if self._adaptive:
                self._adjust_size()
    
    def _adjust_size(self):
        """Adjust bulkhead size based on success rate."""
        total = self._success_count + self._failure_count
        if total < 10:
            return
        
        success_rate = self._success_count / total
        
        if success_rate > 0.95 and self._max_concurrent < self._initial_max * 2:
            # Increase capacity
            self._max_concurrent += 1
            self._semaphore = asyncio.Semaphore(self._max_concurrent)
        elif success_rate < 0.5 and self._max_concurrent > 2:
            # Decrease capacity
            self._max_concurrent -= 1
            self._semaphore = asyncio.Semaphore(self._max_concurrent)
    
    @asynccontextmanager
    async def slot(self, timeout: float = 5.0):
        """Context manager for acquiring a bulkhead slot."""
        acquired = await self.acquire(timeout)
        if not acquired:
            raise Exception(f"Bulkhead '{self._name}' rejected request")
        
        success = True
        try:
            yield
        except Exception:
            success = False
            raise
        finally:
            await self.release(success)
    
    def get_status(self) -> Dict[str, Any]:
        """Get bulkhead status."""
        total = self._success_count + self._failure_count
        return {
            "name": self._name,
            "max_concurrent": self._max_concurrent,
            "active_count": self._active_count,
            "queued_count": self._queued_count,
            "rejected_count": self._rejected_count,
            "success_rate": self._success_count / max(1, total),
        }


# =============================================================================
# Phase 2.2: Async Priority Queue with Preemption
# =============================================================================

class RequestPriority(Enum):
    """Request priority levels."""
    CRITICAL = 0   # System health, auth
    HIGH = 1       # User-initiated actions
    NORMAL = 2     # Regular requests
    LOW = 3        # Background tasks
    BULK = 4       # Batch operations


@dataclass(order=True)
class PrioritizedRequest:
    """Request wrapper with priority ordering."""
    priority: int
    timestamp: float = field(compare=False)
    request_id: str = field(compare=False)
    func: Callable = field(compare=False)
    args: tuple = field(compare=False)
    kwargs: dict = field(compare=False)
    future: asyncio.Future = field(compare=False)


class AsyncPriorityQueue:
    """
    Priority queue with preemption for request scheduling.
    
    Features:
    - **Priority levels**: CRITICAL > HIGH > NORMAL > LOW > BULK
    - **Aging**: Low-priority requests get boosted over time
    - **Preemption**: Critical requests can interrupt BULK
    - **Fair scheduling**: Round-robin within same priority
    """
    
    def __init__(self, max_size: int = 1000):
        self._queue: List[PrioritizedRequest] = []
        self._max_size = max_size
        self._lock = asyncio.Lock()
        self._not_empty = asyncio.Event()
        self._request_count = 0
        self._aging_interval = 30.0  # Boost priority every 30s
    
    async def enqueue(
        self,
        func: Callable,
        priority: RequestPriority = RequestPriority.NORMAL,
        *args,
        **kwargs,
    ) -> asyncio.Future:
        """
        Add request to queue with priority.
        
        Returns:
            Future that will contain the result
        """
        async with self._lock:
            if len(self._queue) >= self._max_size:
                # Shed lowest priority
                if self._queue:
                    self._queue.sort()
                    shed = self._queue.pop()
                    shed.future.set_exception(Exception("Request shed due to queue overflow"))
            
            self._request_count += 1
            request_id = f"req_{self._request_count}_{time.time()}"
            future = asyncio.get_event_loop().create_future()
            
            request = PrioritizedRequest(
                priority=priority.value,
                timestamp=time.time(),
                request_id=request_id,
                func=func,
                args=args,
                kwargs=kwargs,
                future=future,
            )
            
            # Insert maintaining heap order
            import heapq
            heapq.heappush(self._queue, request)
            self._not_empty.set()
            
            return future
    
    async def dequeue(self, timeout: float = 10.0) -> Optional[PrioritizedRequest]:
        """Get next request from queue."""
        try:
            await asyncio.wait_for(self._not_empty.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        
        async with self._lock:
            if not self._queue:
                self._not_empty.clear()
                return None
            
            # Apply aging before dequeue
            self._apply_aging()
            
            import heapq
            request = heapq.heappop(self._queue)
            
            if not self._queue:
                self._not_empty.clear()
            
            return request
    
    def _apply_aging(self):
        """Boost priority of old requests."""
        now = time.time()
        for req in self._queue:
            age = now - req.timestamp
            if age > self._aging_interval and req.priority > 0:
                # Boost priority (lower number = higher priority)
                req.priority = max(0, req.priority - 1)
    
    def get_status(self) -> Dict[str, Any]:
        """Get queue status."""
        priority_counts = {}
        for req in self._queue:
            p = RequestPriority(req.priority).name
            priority_counts[p] = priority_counts.get(p, 0) + 1
        
        return {
            "queue_size": len(self._queue),
            "max_size": self._max_size,
            "total_processed": self._request_count,
            "by_priority": priority_counts,
        }


# =============================================================================
# Phase 2.2: Adaptive Timeout Manager
# =============================================================================

class AdaptiveTimeoutManager:
    """
    Manages adaptive timeouts based on service latency history.
    
    Features:
    - **Per-service timeouts**: Different limits for each service
    - **Latency-based adjustment**: Uses P95 + headroom
    - **Model-aware**: Larger models get longer timeouts
    - **Load-aware**: Increase timeout under high load
    """
    
    _instance: Optional["AdaptiveTimeoutManager"] = None
    
    # Base timeouts by service type (seconds)
    DEFAULT_TIMEOUTS = {
        "rag_trinity": 5.0,
        "rag_local": 2.0,
        "llm_claude": 30.0,
        "llm_llama": 60.0,
        "health_check": 3.0,
        "default": 10.0,
    }
    
    def __init__(self):
        self._config = DynamicConfigManager.get_instance()
        self._latency_history: Dict[str, List[float]] = {}
        self._adaptive_timeouts: Dict[str, float] = {}
        self._lock = asyncio.Lock()
        
        logger.info("[AdaptiveTimeoutManager] Initialized")
    
    @classmethod
    def get_instance(cls) -> "AdaptiveTimeoutManager":
        """Get singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def record_latency(self, service: str, latency: float):
        """Record a latency observation for a service."""
        if service not in self._latency_history:
            self._latency_history[service] = []
        
        self._latency_history[service].append(latency)
        
        # Keep last 100 samples
        if len(self._latency_history[service]) > 100:
            self._latency_history[service] = self._latency_history[service][-100:]
        
        # Update adaptive timeout
        self._update_timeout(service)
    
    def _update_timeout(self, service: str):
        """Update adaptive timeout based on latency history."""
        history = self._latency_history.get(service, [])
        if len(history) < 10:
            return
        
        # Calculate P95
        sorted_latencies = sorted(history)
        p95 = sorted_latencies[int(len(sorted_latencies) * 0.95)]
        
        # Adaptive timeout = P95 + 50% headroom
        base = self.DEFAULT_TIMEOUTS.get(service, self.DEFAULT_TIMEOUTS["default"])
        adaptive = max(base, p95 * 1.5)
        
        # Cap at 3x base
        self._adaptive_timeouts[service] = min(adaptive, base * 3)
    
    def get_timeout(
        self,
        service: str,
        model: Optional[str] = None,
        load_factor: float = 1.0,
    ) -> float:
        """
        Get timeout for a service.
        
        Args:
            service: Service name
            model: Optional model name (for LLM sizing)
            load_factor: Current load (1.0 = normal, 2.0 = high)
            
        Returns:
            Timeout in seconds
        """
        # Start with adaptive or default
        timeout = self._adaptive_timeouts.get(
            service,
            self.DEFAULT_TIMEOUTS.get(service, self.DEFAULT_TIMEOUTS["default"])
        )
        
        # Adjust for model size
        if model:
            if "opus" in model.lower():
                timeout *= 1.5
            elif "sonnet" in model.lower():
                timeout *= 1.2
            elif "70b" in model.lower():
                timeout *= 2.0
            elif "405b" in model.lower():
                timeout *= 3.0
        
        # Adjust for load
        if load_factor > 1.5:
            timeout *= 1.3
        
        return timeout
    
    def get_status(self) -> Dict[str, Any]:
        """Get timeout manager status."""
        return {
            "default_timeouts": self.DEFAULT_TIMEOUTS,
            "adaptive_timeouts": self._adaptive_timeouts.copy(),
            "services_tracked": list(self._latency_history.keys()),
        }


# =============================================================================
# Global accessor functions for Priority 2 components
# =============================================================================

def get_health_aggregator() -> CrossRepoHealthAggregator:
    """Get the cross-repo health aggregator."""
    return CrossRepoHealthAggregator.get_instance()


def get_timeout_manager() -> AdaptiveTimeoutManager:
    """Get the adaptive timeout manager."""
    return AdaptiveTimeoutManager.get_instance()


# =============================================================================
# Phase 3: Chaos Engineering - Controlled Fault Injection
# =============================================================================

class FaultType(Enum):
    """Types of faults that can be injected."""
    LATENCY = "latency"           # Add artificial delay
    ERROR = "error"               # Raise exception
    TIMEOUT = "timeout"           # Force timeout
    PARTIAL_FAILURE = "partial"   # Return degraded result
    CORRUPTION = "corruption"     # Return corrupted data
    CONNECTION_RESET = "reset"    # Simulate connection reset


@dataclass
class FaultConfig:
    """Configuration for a specific fault type."""
    fault_type: FaultType
    probability: float = 0.0      # 0.0-1.0 chance of fault
    latency_ms: float = 0.0       # For LATENCY type
    error_message: str = ""       # For ERROR type
    enabled: bool = False
    target_services: List[str] = field(default_factory=list)  # Empty = all services


class ChaosMonkey:
    """
    Controlled chaos engineering for resilience testing.
    
    Features:
    - **Probability-based faults**: Each request has X% chance of fault
    - **Targeted injection**: Only affect specific services
    - **Scheduled chaos**: Run chaos tests at specific times
    - **Kill switch**: Instantly disable all chaos
    - **Gradual ramp**: Slowly increase fault rate
    
    Usage:
        chaos = ChaosMonkey.get_instance()
        chaos.enable_fault(FaultType.LATENCY, probability=0.1, latency_ms=500)
        
        # In your code:
        await chaos.maybe_inject("rag_trinity")  # 10% chance of 500ms delay
    
    Safety:
    - Disabled by default
    - Requires explicit enable
    - Environment variable kill switch: CHAOS_ENABLED=false
    """
    
    _instance: Optional["ChaosMonkey"] = None
    
    def __init__(self):
        self._enabled = os.getenv("CHAOS_ENABLED", "false").lower() == "true"
        self._fault_configs: Dict[FaultType, FaultConfig] = {}
        self._injection_count = 0
        self._total_requests = 0
        self._lock = asyncio.Lock()
        
        # Scheduled chaos windows (hour ranges when chaos is active)
        self._chaos_windows: List[Tuple[int, int]] = []  # [(start_hour, end_hour), ...]
        
        # Gradual ramp configuration
        self._ramp_enabled = False
        self._ramp_start_time = 0.0
        self._ramp_duration = 300.0  # 5 minutes to reach full probability
        
        logger.info(f"[ChaosMonkey] Initialized (enabled={self._enabled})")
    
    @classmethod
    def get_instance(cls) -> "ChaosMonkey":
        """Get singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def enable(self):
        """Enable chaos monkey."""
        self._enabled = True
        logger.warning("[ChaosMonkey] ENABLED - Faults will be injected!")
    
    def disable(self):
        """Kill switch - disable all chaos immediately."""
        self._enabled = False
        logger.info("[ChaosMonkey] DISABLED - All faults stopped")
    
    def enable_fault(
        self,
        fault_type: FaultType,
        probability: float = 0.1,
        latency_ms: float = 500.0,
        error_message: str = "Chaos injection",
        target_services: Optional[List[str]] = None,
    ):
        """Configure a fault type for injection."""
        self._fault_configs[fault_type] = FaultConfig(
            fault_type=fault_type,
            probability=min(1.0, max(0.0, probability)),
            latency_ms=latency_ms,
            error_message=error_message,
            enabled=True,
            target_services=target_services or [],
        )
        logger.info(f"[ChaosMonkey] Enabled {fault_type.value} (p={probability:.1%})")
    
    def disable_fault(self, fault_type: FaultType):
        """Disable a specific fault type."""
        if fault_type in self._fault_configs:
            self._fault_configs[fault_type].enabled = False
    
    def set_chaos_window(self, start_hour: int, end_hour: int):
        """Set time window when chaos is active (24h format)."""
        self._chaos_windows.append((start_hour % 24, end_hour % 24))
    
    def start_gradual_ramp(self, duration_seconds: float = 300.0):
        """Start gradual ramp-up of fault probability."""
        self._ramp_enabled = True
        self._ramp_start_time = time.time()
        self._ramp_duration = duration_seconds
        logger.info(f"[ChaosMonkey] Starting gradual ramp over {duration_seconds}s")
    
    def _is_in_chaos_window(self) -> bool:
        """Check if current time is in a chaos window."""
        if not self._chaos_windows:
            return True  # No windows = always active
        
        current_hour = int(time.time() // 3600) % 24
        for start, end in self._chaos_windows:
            if start <= end:
                if start <= current_hour < end:
                    return True
            else:  # Wraps around midnight
                if current_hour >= start or current_hour < end:
                    return True
        return False
    
    def _get_effective_probability(self, base_probability: float) -> float:
        """Get effective probability considering ramp."""
        if not self._ramp_enabled:
            return base_probability
        
        elapsed = time.time() - self._ramp_start_time
        ramp_factor = min(1.0, elapsed / self._ramp_duration)
        return base_probability * ramp_factor
    
    async def maybe_inject(self, service: str) -> Optional[FaultType]:
        """
        Maybe inject a fault for this request.
        
        Returns:
            FaultType if fault was injected, None otherwise
        """
        self._total_requests += 1
        
        # Check kill switches
        if not self._enabled:
            return None
        if not self._is_in_chaos_window():
            return None
        
        # Check each enabled fault
        for fault_type, config in self._fault_configs.items():
            if not config.enabled:
                continue
            
            # Check service targeting
            if config.target_services and service not in config.target_services:
                continue
            
            # Roll dice
            effective_prob = self._get_effective_probability(config.probability)
            if random.random() < effective_prob:
                self._injection_count += 1
                await self._inject_fault(config)
                return fault_type
        
        return None
    
    async def _inject_fault(self, config: FaultConfig):
        """Actually inject the fault."""
        if config.fault_type == FaultType.LATENCY:
            await asyncio.sleep(config.latency_ms / 1000.0)
            logger.debug(f"[ChaosMonkey] Injected {config.latency_ms}ms latency")
            
        elif config.fault_type == FaultType.ERROR:
            raise Exception(f"[ChaosMonkey] {config.error_message}")
            
        elif config.fault_type == FaultType.TIMEOUT:
            await asyncio.sleep(120)  # Force timeout
            
        elif config.fault_type == FaultType.CONNECTION_RESET:
            raise ConnectionResetError("[ChaosMonkey] Connection reset")
    
    def get_status(self) -> Dict[str, Any]:
        """Get chaos monkey status."""
        return {
            "enabled": self._enabled,
            "in_chaos_window": self._is_in_chaos_window(),
            "injection_count": self._injection_count,
            "total_requests": self._total_requests,
            "injection_rate": self._injection_count / max(1, self._total_requests),
            "active_faults": [
                {
                    "type": ft.value,
                    "probability": cfg.probability,
                    "enabled": cfg.enabled,
                }
                for ft, cfg in self._fault_configs.items()
            ],
        }


# =============================================================================
# Phase 3: Security Layer - Request Validation & Sanitization
# =============================================================================

class SecurityViolation(Exception):
    """Raised when a security check fails."""
    pass


class SecurityLayer:
    """
    Security hardening layer for request validation.
    
    Features:
    - **Input sanitization**: Prevent injection attacks
    - **Size limits**: Reject oversized requests
    - **Pattern detection**: Detect suspicious patterns
    - **Rate limiting**: Per-client request limits
    - **Request signing**: HMAC verification for cross-repo calls
    """
    
    _instance: Optional["SecurityLayer"] = None
    
    # Dangerous patterns to detect
    DANGEROUS_PATTERNS = [
        re.compile(r'<script.*?>.*?</script>', re.I | re.S),  # XSS
        re.compile(r';\s*DROP\s+TABLE', re.I),                 # SQL injection
        re.compile(r'__import__\s*\('),                        # Python exec
        re.compile(r'eval\s*\('),                              # Eval
        re.compile(r'\$\{.*?\}'),                              # Template injection
        re.compile(r'{{.*?}}'),                                # Template injection
    ]
    
    def __init__(self):
        self._config = DynamicConfigManager.get_instance()
        
        # Limits (configurable via env)
        self._max_query_length = int(os.getenv("SECURITY_MAX_QUERY_LENGTH", "10000"))
        self._max_context_length = int(os.getenv("SECURITY_MAX_CONTEXT_LENGTH", "100000"))
        
        # HMAC key for request signing
        self._hmac_key = os.getenv("JARVIS_HMAC_KEY", "").encode() or os.urandom(32)
        
        # Violation tracking
        self._violation_count = 0
        self._violations_by_type: Dict[str, int] = {}
        
        logger.info("[SecurityLayer] Initialized")
    
    @classmethod
    def get_instance(cls) -> "SecurityLayer":
        """Get singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def validate_query(self, query: str) -> str:
        """
        Validate and sanitize a user query.
        
        Args:
            query: Raw user query
            
        Returns:
            Sanitized query
            
        Raises:
            SecurityViolation: If query fails validation
        """
        if not isinstance(query, str):
            self._record_violation("invalid_type")
            raise SecurityViolation("Query must be a string")
        
        # Length check
        if len(query) > self._max_query_length:
            self._record_violation("query_too_long")
            raise SecurityViolation(f"Query exceeds max length ({self._max_query_length})")
        
        # Pattern detection
        for pattern in self.DANGEROUS_PATTERNS:
            if pattern.search(query):
                self._record_violation("dangerous_pattern")
                raise SecurityViolation("Query contains suspicious content")
        
        # Sanitize
        sanitized = self._sanitize(query)
        return sanitized
    
    def validate_context(self, context: str) -> str:
        """Validate and sanitize context data."""
        if len(context) > self._max_context_length:
            self._record_violation("context_too_long")
            # Truncate instead of reject
            return context[:self._max_context_length]
        return self._sanitize(context)
    
    def _sanitize(self, text: str) -> str:
        """Sanitize text by removing/escaping dangerous content."""
        # Remove null bytes
        text = text.replace('\x00', '')
        
        # Remove control characters (except newline, tab)
        text = ''.join(
            c for c in text
            if c in '\n\t' or (ord(c) >= 32 and ord(c) < 127) or ord(c) >= 128
        )
        
        return text.strip()
    
    def _record_violation(self, violation_type: str):
        """Record a security violation."""
        self._violation_count += 1
        self._violations_by_type[violation_type] = (
            self._violations_by_type.get(violation_type, 0) + 1
        )
        logger.warning(f"[SecurityLayer] Violation: {violation_type}")
    
    def sign_request(self, payload: str, timestamp: float) -> str:
        """
        Sign a request for cross-repo authentication.
        
        Args:
            payload: Request payload
            timestamp: Unix timestamp
            
        Returns:
            HMAC signature (hex)
        """
        import hmac as hmac_module
        message = f"{timestamp}:{payload}".encode()
        signature = hmac_module.new(self._hmac_key, message, hashlib.sha256).hexdigest()
        return signature
    
    def verify_signature(
        self,
        payload: str,
        timestamp: float,
        signature: str,
        max_age_seconds: float = 300.0,
    ) -> bool:
        """
        Verify a request signature.
        
        Args:
            payload: Request payload
            timestamp: Claimed timestamp
            signature: Claimed signature
            max_age_seconds: Max age of request (replay protection)
            
        Returns:
            True if valid
        """
        # Check age
        age = time.time() - timestamp
        if age > max_age_seconds or age < -60:  # Allow 60s clock skew
            return False
        
        # Verify signature
        expected = self.sign_request(payload, timestamp)
        import hmac as hmac_module
        return hmac_module.compare_digest(signature, expected)
    
    def get_status(self) -> Dict[str, Any]:
        """Get security layer status."""
        return {
            "violation_count": self._violation_count,
            "violations_by_type": self._violations_by_type.copy(),
            "max_query_length": self._max_query_length,
            "max_context_length": self._max_context_length,
        }


# =============================================================================
# Phase 3: Rate Limit Guard - Per-Client Throttling
# =============================================================================

class RateLimitGuard:
    """
    Per-client rate limiting with sliding window.
    
    Features:
    - **Sliding window**: More accurate than fixed window
    - **Per-client**: Track by client_id
    - **Tiered limits**: Different limits for different tiers
    - **Burst allowance**: Allow short bursts
    - **Gradual recovery**: Smooth rate limit recovery
    """
    
    _instance: Optional["RateLimitGuard"] = None
    
    # Default limits per tier (requests per minute)
    DEFAULT_LIMITS = {
        "system": 1000,     # Internal system calls
        "premium": 100,     # Premium users
        "standard": 30,     # Standard users
        "anonymous": 10,    # Anonymous/unknown
    }
    
    def __init__(self):
        self._limits = self.DEFAULT_LIMITS.copy()
        
        # Override from env
        for tier in self._limits:
            env_key = f"RATE_LIMIT_{tier.upper()}"
            if os.getenv(env_key):
                self._limits[tier] = int(os.getenv(env_key))
        
        # Sliding window state: client_id -> [(timestamp, count), ...]
        self._windows: Dict[str, List[Tuple[float, int]]] = {}
        self._window_size = 60.0  # 60 second window
        self._lock = asyncio.Lock()
        
        # Metrics
        self._allowed_count = 0
        self._rejected_count = 0
        
        logger.info("[RateLimitGuard] Initialized")
    
    @classmethod
    def get_instance(cls) -> "RateLimitGuard":
        """Get singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    async def check_rate_limit(
        self,
        client_id: str,
        tier: str = "standard",
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Check if request is allowed under rate limit.
        
        Args:
            client_id: Unique client identifier
            tier: Client tier for limit lookup
            
        Returns:
            Tuple of (allowed, rate_limit_info)
        """
        async with self._lock:
            now = time.time()
            limit = self._limits.get(tier, self._limits["anonymous"])
            
            # Get or create window
            if client_id not in self._windows:
                self._windows[client_id] = []
            
            window = self._windows[client_id]
            
            # Remove old entries
            window = [(t, c) for t, c in window if now - t < self._window_size]
            
            # Count current requests
            current_count = sum(c for _, c in window)
            
            # Check limit
            remaining = max(0, limit - current_count)
            reset_time = now + self._window_size
            
            if current_count >= limit:
                self._rejected_count += 1
                return False, {
                    "allowed": False,
                    "limit": limit,
                    "remaining": 0,
                    "reset_at": reset_time,
                    "retry_after": self._window_size,
                }
            
            # Record this request
            window.append((now, 1))
            self._windows[client_id] = window
            self._allowed_count += 1
            
            return True, {
                "allowed": True,
                "limit": limit,
                "remaining": remaining - 1,
                "reset_at": reset_time,
            }
    
    async def cleanup_old_windows(self):
        """Clean up stale client windows."""
        async with self._lock:
            now = time.time()
            stale_clients = []
            
            for client_id, window in self._windows.items():
                if not window or now - window[-1][0] > self._window_size * 2:
                    stale_clients.append(client_id)
            
            for client_id in stale_clients:
                del self._windows[client_id]
    
    def get_status(self) -> Dict[str, Any]:
        """Get rate limiter status."""
        return {
            "limits": self._limits.copy(),
            "active_clients": len(self._windows),
            "allowed_count": self._allowed_count,
            "rejected_count": self._rejected_count,
            "rejection_rate": self._rejected_count / max(1, self._allowed_count + self._rejected_count),
        }


# =============================================================================
# Global accessor functions for Phase 3 components
# =============================================================================

def get_chaos_monkey() -> ChaosMonkey:
    """Get the chaos monkey instance."""
    return ChaosMonkey.get_instance()


def get_security_layer() -> SecurityLayer:
    """Get the security layer instance."""
    return SecurityLayer.get_instance()


def get_rate_limit_guard() -> RateLimitGuard:
    """Get the rate limit guard instance."""
    return RateLimitGuard.get_instance()


# Example usage
if __name__ == "__main__":
    async def test_rag():
        # Initialize RAG engine
        rag = RAGEngine()
        
        # Add some knowledge
        await rag.add_knowledge(
            "Python is a high-level programming language known for its simplicity and readability.",
            {"source": "tutorial", "topic": "programming"}
        )
        
        await rag.add_knowledge(
            "Machine learning is a subset of AI that enables systems to learn from data.",
            {"source": "textbook", "topic": "AI"}
        )
        
        # Test retrieval
        result = await rag.generate_with_retrieval("Tell me about Python programming")
        print(f"Response: {result['response']}")
        print(f"Context used: {result['context_used'][:100]}...")
        
        # Test conversation summarization
        messages = [
            {"role": "user", "content": "What is machine learning?"},
            {"role": "assistant", "content": "Machine learning is a subset of AI..."},
            {"role": "user", "content": "Can you give me an example?"},
            {"role": "assistant", "content": "Sure! Image recognition is a common example..."}
        ]
        
        summary = await rag.summarize_conversation(messages)
        print(f"\nSummary: {summary.summary}")
        print(f"Key points: {summary.key_points}")
        print(f"Topics: {summary.topics}")
        
    # Run test
    asyncio.run(test_rag())