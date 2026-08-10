"""LangChain-compatible Mistral embeddings for Mem0 (no OpenAI dimensions param)."""
from typing import List

from langchain_core.embeddings import Embeddings
from openai import OpenAI

MISTRAL_API_BASE = "https://api.mistral.ai/v1"
DEFAULT_MODEL = "mistral-embed"


class MistralEmbeddings(Embeddings):
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=MISTRAL_API_BASE)

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text: str) -> List[float]:
        text = text.replace("\n", " ")
        response = self.client.embeddings.create(input=[text], model=self.model)
        return response.data[0].embedding
