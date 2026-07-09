from typing import Optional, Dict, Any, Literal
from datetime import datetime
from pydantic import BaseModel,Field

MessageRole = Literal["user","assistant","system","tool","summary"]

class Message(BaseModel):
    content:str
    role:MessageRole
    timestamp:datetime = Field(default_factory=datetime.now)
    metadata:Optional[Dict[str,Any]] = Field(default_factory=dict)

    def to_dict(self) -> Dict[str,Any]:
        return {
            "role":self.role,
            "content":self.content,
            "timestamp":self.timestamp.isoformat() if self.timestamp else None,
            "metadata":self.metadata
        }
    
    @classmethod
    def from_dict(cls,data:Dict[str,Any]) -> "Message":
        timestamp = data.get("timestamp")
        if timestamp and isinstance(timestamp,str):
            timestamp = datetime.fromisoformat(timestamp)

        kwargs = {
            "content":data["content"],
            "role":data["role"],
            "metadata":data.get("metadata",{})
        }

        if timestamp is not None:
            kwargs["timestamp"] = timestamp
        
        return cls(**kwargs)
    
    def to_text(self) -> str:
        return f"[{self.role}]{self.content}"
    
    def __str__(self) -> str:
        return f"[{self.role}]{self.content}"
