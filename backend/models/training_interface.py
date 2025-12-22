import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Fix TensorFlow import issues before any other imports
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'  # Disable TF warnings
os.environ['USE_TORCH'] = '1'  # Use PyTorch backend for transformers
os.environ['USE_TF'] = '0'     # Disable TensorFlow in transformers

# Apply TensorFlow fixes
try:
    import tensorflow as tf
    if not hasattr(tf, 'data'):
        # Create a mock data module to prevent import errors
        class MockData:
            class Dataset:
                @staticmethod
                def from_tensor_slices(*args, **kwargs):
                    return None
        tf.data = MockData()
except Exception:
    pass  # TensorFlow not required for core functionality

from fastapi import FastAPI, APIRouter, BackgroundTasks, HTTPException, UploadFile, File
from pydantic import BaseModel, Field
from typing import Dict, List, Optional, Any, Union
import json
import os
from pathlib import Path
import asyncio
from datetime import datetime
import shutil
import torch
from dataclasses import asdict

from models.custom_model import CustomChatbotModel, CustomChatbotConfig, ModelBuilder
from models.training_pipeline import TrainingConfig, ModelTrainer
from models.fine_tuning import FineTuneConfig, FineTuner, DomainSpecificFineTuner
from utils.evaluation_metrics import ModelEvaluator, EvaluationResult
from utils.domain_knowledge import DomainKnowledgeBank, DomainKnowledge

class TrainingRequest(BaseModel):
    """Request model for training"""
    model_size: str = Field(default="small", description="Model size: small, base, large")
    num_epochs: int = Field(default=3, description="Number of training epochs")
    batch_size: int = Field(default=8, description="Training batch size")
    learning_rate: float = Field(default=5e-5, description="Learning rate")
    train_data_path: Optional[str] = Field(default=None, description="Path to training data")
    eval_data_path: Optional[str] = Field(default=None, description="Path to evaluation data")
    use_curriculum_learning: bool = Field(default=True, description="Use curriculum learning")
    use_data_augmentation: bool = Field(default=True, description="Use data augmentation")
    

class FineTuneRequest(BaseModel):
    """Request model for fine-tuning"""
    base_model_path: str = Field(description="Path to base model")
    domain: Optional[str] = Field(default=None, description="Domain for specialization")
    num_epochs: int = Field(default=3, description="Number of fine-tuning epochs")
    batch_size: int = Field(default=4, description="Fine-tuning batch size")
    learning_rate: float = Field(default=2e-5, description="Learning rate")
    use_lora: bool = Field(default=True, description="Use LoRA for efficient fine-tuning")
    lora_rank: int = Field(default=8, description="LoRA rank")
    use_adapters: bool = Field(default=True, description="Use adapter layers")
    adapter_size: int = Field(default=64, description="Adapter size")
    

class EvaluationRequest(BaseModel):
    """Request model for evaluation"""
    model_path: str = Field(description="Path to model to evaluate")
    eval_data_path: str = Field(description="Path to evaluation data")
    compute_generation_metrics: bool = Field(default=True, description="Compute generation metrics")
    max_generation_length: int = Field(default=100, description="Max generation length")
    

class DomainKnowledgeRequest(BaseModel):
    """Request model for domain knowledge"""
    domain: str = Field(description="Domain name")
    facts: List[str] = Field(default_factory=list, description="Domain facts")
    rules: List[Dict[str, Any]] = Field(default_factory=list, description="Domain rules")
    examples: List[Dict[str, str]] = Field(default_factory=list, description="Domain examples")
    terminology: Dict[str, str] = Field(default_factory=dict, description="Domain terminology")
    constraints: List[str] = Field(default_factory=list, description="Domain constraints")

class TrainingStatus(BaseModel):
    """Training status response"""
    job_id: str
    status: str  # pending, running, completed, failed
    progress: float  # 0.0 to 1.0
    current_epoch: Optional[int] = None
    current_step: Optional[int] = None
    current_loss: Optional[float] = None
    eval_loss: Optional[float] = None
    message: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    output_path: Optional[str] = None

class TrainingInterface:
    """API interface for model training and fine-tuning"""
    
    def __init__(self):
        self.router = APIRouter()
        self.training_jobs: Dict[str, TrainingStatus] = {}
        self.knowledge_bank = DomainKnowledgeBank()
        
        # Setup routes
        self._setup_routes()
        
        # Ensure directories exist
        os.makedirs("./models", exist_ok=True)
        os.makedirs("./checkpoints", exist_ok=True)
        os.makedirs("./data", exist_ok=True)
        os.makedirs("./logs", exist_ok=True)
        
    def _setup_routes(self):
        """Setup API routes"""
        # Training routes
        self.router.add_api_route("/train", self.start_training, methods=["POST"])
        self.router.add_api_route("/train/{job_id}/status", self.get_training_status, methods=["GET"])
        self.router.add_api_route("/train/{job_id}/stop", self.stop_training, methods=["POST"])
        
        # Fine-tuning routes
        self.router.add_api_route("/fine-tune", self.start_fine_tuning, methods=["POST"])
        
        # Evaluation routes
        self.router.add_api_route("/evaluate", self.evaluate_model, methods=["POST"])
        
        # Domain knowledge routes
        self.router.add_api_route("/domains", self.add_domain_knowledge, methods=["POST"])
        self.router.add_api_route("/domains/{domain}", self.get_domain_knowledge, methods=["GET"])
        self.router.add_api_route("/domains", self.list_domains, methods=["GET"])
        
        # Model management routes
        self.router.add_api_route("/models", self.list_models, methods=["GET"])
        self.router.add_api_route("/models/{model_name}/info", self.get_model_info, methods=["GET"])
        self.router.add_api_route("/models/{model_name}/export", self.export_model, methods=["POST"])
        
        # Data management routes
        self.router.add_api_route("/data/upload", self.upload_training_data, methods=["POST"])
        self.router.add_api_route("/data/generate", self.generate_synthetic_data, methods=["POST"])
        
    async def start_training(
        self, 
        request: TrainingRequest,
        background_tasks: BackgroundTasks
    ) -> TrainingStatus:
        """Start a new training job"""
        # Generate job ID
        job_id = f"train_{datetime.now():%Y%m%d_%H%M%S}"
        
        # Create training config
        config = TrainingConfig(
            model_size=request.model_size,
            num_epochs=request.num_epochs,
            batch_size=request.batch_size,
            learning_rate=request.learning_rate,
            train_data_path=request.train_data_path or "./data/train.jsonl",
            eval_data_path=request.eval_data_path or "./data/eval.jsonl",
            use_curriculum_learning=request.use_curriculum_learning,
            use_data_augmentation=request.use_data_augmentation,
            output_dir=f"./models/{job_id}",
            checkpoint_dir=f"./checkpoints/{job_id}",
            use_wandb=False  # Disable for API usage
        )
        
        # Create status
        status = TrainingStatus(
            job_id=job_id,
            status="pending",
            progress=0.0,
            message="Training job queued",
            started_at=datetime.now().isoformat()
        )
        self.training_jobs[job_id] = status
        
        # Start training in background
        background_tasks.add_task(
            self._run_training,
            job_id,
            config
        )
        
        return status
        
    async def _run_training(self, job_id: str, config: TrainingConfig):
        """Run training job"""
        status = self.training_jobs[job_id]
        status.status = "running"
        status.message = "Training started"
        
        try:
            # Create trainer
            trainer = ModelTrainer(config)
            
            # Update status callback
            def update_status(epoch, step, loss, eval_loss=None):
                status.current_epoch = epoch
                status.current_step = step
                status.current_loss = loss
                status.eval_loss = eval_loss
                status.progress = step / (config.num_epochs * 1000)  # Approximate
                
            # Monkey patch the trainer to update status
            original_train_epoch = trainer.train_epoch
            
            def train_epoch_with_status(dataloader, epoch):
                result = original_train_epoch(dataloader, epoch)
                update_status(epoch, trainer.global_step, result)
                return result
                
            trainer.train_epoch = train_epoch_with_status
            
            # Run training
            trainer.train()
            
            # Update status
            status.status = "completed"
            status.progress = 1.0
            status.message = "Training completed successfully"
            status.completed_at = datetime.now().isoformat()
            status.output_path = config.output_dir
            
        except Exception as e:
            status.status = "failed"
            status.message = f"Training failed: {str(e)}"
            status.completed_at = datetime.now().isoformat()
            
    async def get_training_status(self, job_id: str) -> TrainingStatus:
        """Get training job status"""
        if job_id not in self.training_jobs:
            raise HTTPException(status_code=404, detail="Training job not found")
        return self.training_jobs[job_id]
        
    async def stop_training(self, job_id: str) -> Dict[str, str]:
        """Stop a training job"""
        if job_id not in self.training_jobs:
            raise HTTPException(status_code=404, detail="Training job not found")
            
        status = self.training_jobs[job_id]
        if status.status == "running":
            # In practice, implement proper cancellation
            status.status = "cancelled"
            status.message = "Training cancelled by user"
            status.completed_at = datetime.now().isoformat()
            
        return {"message": f"Training job {job_id} stopped"}
        
    async def start_fine_tuning(
        self,
        request: FineTuneRequest,
        background_tasks: BackgroundTasks
    ) -> TrainingStatus:
        """Start fine-tuning job"""
        # Generate job ID
        job_id = f"finetune_{datetime.now():%Y%m%d_%H%M%S}"
        
        # Create fine-tune config
        config = FineTuneConfig(
            base_model_path=request.base_model_path,
            num_epochs=request.num_epochs,
            batch_size=request.batch_size,
            learning_rate=request.learning_rate,
            use_lora=request.use_lora,
            lora_rank=request.lora_rank,
            adapter_tuning=request.use_adapters,
            adapter_size=request.adapter_size,
            output_dir=f"./models/{job_id}"
        )
        
        # Create status
        status = TrainingStatus(
            job_id=job_id,
            status="pending",
            progress=0.0,
            message="Fine-tuning job queued",
            started_at=datetime.now().isoformat()
        )
        self.training_jobs[job_id] = status
        
        # Start fine-tuning in background
        background_tasks.add_task(
            self._run_fine_tuning,
            job_id,
            config,
            request.domain
        )
        
        return status
        
    async def _run_fine_tuning(
        self, 
        job_id: str, 
        config: FineTuneConfig,
        domain: Optional[str] = None
    ):
        """Run fine-tuning job"""
        status = self.training_jobs[job_id]
        status.status = "running"
        status.message = "Fine-tuning started"
        
        try:
            # Create fine-tuner
            if domain:
                fine_tuner = DomainSpecificFineTuner(config, domain)
            else:
                fine_tuner = FineTuner(config)
                
            # Run fine-tuning
            fine_tuner.fine_tune()
            
            # Update status
            status.status = "completed"
            status.progress = 1.0
            status.message = "Fine-tuning completed successfully"
            status.completed_at = datetime.now().isoformat()
            status.output_path = config.output_dir
            
        except Exception as e:
            status.status = "failed"
            status.message = f"Fine-tuning failed: {str(e)}"
            status.completed_at = datetime.now().isoformat()
            
    async def evaluate_model(self, request: EvaluationRequest) -> Dict[str, Any]:
        """Evaluate a model"""
        try:
            # Load model
            from transformers import AutoTokenizer
            
            if os.path.exists(os.path.join(request.model_path, "config.json")):
                # Custom model
                model = CustomChatbotModel.from_pretrained(request.model_path)
            else:
                # Standard model
                from transformers import AutoModelForCausalLM
                model = AutoModelForCausalLM.from_pretrained(request.model_path)
                
            tokenizer = AutoTokenizer.from_pretrained(request.model_path)
            
            # Create evaluator
            evaluator = ModelEvaluator(tokenizer)
            
            # Load evaluation data
            from models.training_pipeline import ConversationDataset
            from torch.utils.data import DataLoader
            
            eval_dataset = ConversationDataset(
                request.eval_data_path,
                tokenizer,
                max_length=512
            )
            
            eval_dataloader = DataLoader(
                eval_dataset,
                batch_size=8,
                shuffle=False
            )
            
            # Run evaluation
            results = evaluator.evaluate_model(
                model,
                eval_dataloader,
                compute_generation_metrics=request.compute_generation_metrics,
                max_generation_length=request.max_generation_length
            )
            
            # Convert to dict
            return asdict(results)
            
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
            
    async def add_domain_knowledge(self, request: DomainKnowledgeRequest) -> Dict[str, str]:
        """Add domain knowledge"""
        try:
            knowledge = DomainKnowledge(
                domain=request.domain,
                facts=request.facts,
                rules=request.rules,
                examples=request.examples,
                terminology=request.terminology,
                constraints=request.constraints
            )
            
            self.knowledge_bank.add_domain(request.domain, knowledge)
            
            # Save to file
            os.makedirs("./domain_knowledge", exist_ok=True)
            self.knowledge_bank.save_to_file(f"./domain_knowledge/{request.domain}.json")
            
            return {
                "message": f"Domain knowledge added for {request.domain}",
                "facts_count": len(request.facts),
                "rules_count": len(request.rules),
                "examples_count": len(request.examples)
            }
            
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
            
    async def get_domain_knowledge(self, domain: str) -> Dict[str, Any]:
        """Get domain knowledge"""
        if domain not in self.knowledge_bank.domains:
            # Try loading from file
            knowledge_file = f"./domain_knowledge/{domain}.json"
            if os.path.exists(knowledge_file):
                self.knowledge_bank.load_from_file(knowledge_file)
            else:
                raise HTTPException(status_code=404, detail="Domain not found")
                
        knowledge = self.knowledge_bank.domains[domain]
        
        return {
            "domain": domain,
            "facts": knowledge.facts,
            "rules": knowledge.rules,
            "examples": knowledge.examples,
            "terminology": knowledge.terminology,
            "constraints": knowledge.constraints,
            "facts_count": len(knowledge.facts),
            "has_embeddings": knowledge.embeddings is not None
        }
        
    async def list_domains(self) -> List[str]:
        """List available domains"""
        # Get domains from memory
        domains = list(self.knowledge_bank.domains.keys())
        
        # Check for additional domains on disk
        if os.path.exists("./domain_knowledge"):
            for file in os.listdir("./domain_knowledge"):
                if file.endswith(".json"):
                    domain = file[:-5]  # Remove .json
                    if domain not in domains:
                        domains.append(domain)
                        
        return domains
        
    async def list_models(self) -> List[Dict[str, Any]]:
        """List available models"""
        models = []
        
        if os.path.exists("./models"):
            for model_dir in os.listdir("./models"):
                model_path = os.path.join("./models", model_dir)
                if os.path.isdir(model_path):
                    info = {
                        "name": model_dir,
                        "path": model_path,
                        "created": datetime.fromtimestamp(
                            os.path.getctime(model_path)
                        ).isoformat()
                    }
                    
                    # Check for config
                    config_path = os.path.join(model_path, "config.json")
                    if os.path.exists(config_path):
                        with open(config_path, 'r') as f:
                            config = json.load(f)
                            info["type"] = config.get("model_type", "unknown")
                            info["parameters"] = config.get("num_parameters")
                            
                    models.append(info)
                    
        return models
        
    async def get_model_info(self, model_name: str) -> Dict[str, Any]:
        """Get detailed model information"""
        model_path = os.path.join("./models", model_name)
        
        if not os.path.exists(model_path):
            raise HTTPException(status_code=404, detail="Model not found")
            
        info = {
            "name": model_name,
            "path": model_path,
            "size_mb": sum(
                os.path.getsize(os.path.join(model_path, f))
                for f in os.listdir(model_path)
            ) / (1024 * 1024)
        }
        
        # Load config
        config_path = os.path.join(model_path, "config.json")
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                info["config"] = json.load(f)
                
        # Load training config if available
        train_config_path = os.path.join(model_path, "training_config.json")
        if os.path.exists(train_config_path):
            with open(train_config_path, 'r') as f:
                info["training_config"] = json.load(f)
                
        return info
        
    async def export_model(self, model_name: str, format: str = "pytorch") -> Dict[str, str]:
        """Export model in specified format"""
        model_path = os.path.join("./models", model_name)
        
        if not os.path.exists(model_path):
            raise HTTPException(status_code=404, detail="Model not found")
            
        export_path = os.path.join("./exports", model_name)
        os.makedirs(export_path, exist_ok=True)
        
        if format == "pytorch":
            # Already in PyTorch format
            shutil.copytree(model_path, export_path, dirs_exist_ok=True)
            
        elif format == "onnx":
            # Export to ONNX
            # This is simplified - implement proper ONNX export
            raise HTTPException(status_code=501, detail="ONNX export not implemented")
            
        else:
            raise HTTPException(status_code=400, detail=f"Unknown format: {format}")
            
        return {
            "message": f"Model exported successfully",
            "format": format,
            "path": export_path
        }
        
    async def upload_training_data(self, file: UploadFile = File(...)) -> Dict[str, Any]:
        """Upload training data"""
        # Validate file type
        if not file.filename.endswith(('.jsonl', '.json')):
            raise HTTPException(
                status_code=400,
                detail="Only .json and .jsonl files are supported"
            )
            
        # Save file
        file_path = os.path.join("./data", file.filename)
        
        with open(file_path, 'wb') as f:
            content = await file.read()
            f.write(content)
            
        # Validate content
        line_count = 0
        try:
            with open(file_path, 'r') as f:
                for line in f:
                    json.loads(line)
                    line_count += 1
        except json.JSONDecodeError as e:
            os.remove(file_path)
            raise HTTPException(
                status_code=400,
                detail=f"Invalid JSON format: {str(e)}"
            )
            
        return {
            "message": "Data uploaded successfully",
            "filename": file.filename,
            "path": file_path,
            "examples": line_count
        }
        
    async def generate_synthetic_data(
        self,
        num_examples: int = 100,
        domain: Optional[str] = None
    ) -> Dict[str, Any]:
        """Generate synthetic training data"""
        # This is a placeholder - implement actual generation
        synthetic_data = []
        
        templates = [
            ("Hello!", "Hi there! How can I help you today?"),
            ("What's the weather like?", "I'd be happy to help with weather information. Could you tell me which location you're interested in?"),
            ("Thank you!", "You're welcome! Is there anything else I can help you with?"),
        ]
        
        for i in range(num_examples):
            template = templates[i % len(templates)]
            example = {
                "id": f"synthetic_{i}",
                "messages": [
                    {"role": "user", "content": template[0]},
                    {"role": "assistant", "content": template[1]}
                ]
            }
            
            if domain:
                example["domain"] = domain
                
            synthetic_data.append(example)
            
        # Save to file
        file_path = f"./data/synthetic_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
        
        with open(file_path, 'w') as f:
            for example in synthetic_data:
                f.write(json.dumps(example) + '\n')
                
        return {
            "message": "Synthetic data generated",
            "path": file_path,
            "examples": num_examples
        }

# Example usage
# Create interface
interface = TrainingInterface()

# Create FastAPI app
app = FastAPI(title="Model Training Interface")
app.include_router(interface.router, prefix="/training")

@app.get("/")
async def root():
    return {
        "message": "Model Training Interface",
        "endpoints": {
            "/training/train": "Start training job",
            "/training/fine-tune": "Start fine-tuning job", 
            "/training/evaluate": "Evaluate model",
            "/training/domains": "Manage domain knowledge",
            "/training/models": "List available models"
        }
    }

if __name__ == "__main__":
    # Run server
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)