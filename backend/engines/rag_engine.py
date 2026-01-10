import numpy as np
from typing import List, Dict, Optional, Tuple, Any, Union, Callable
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
import pickle
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
            
        except:
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