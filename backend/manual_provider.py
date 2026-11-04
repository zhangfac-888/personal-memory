"""The manual review workflow shares the configured cloud transport, never local generation."""
import hashlib,json
from pathlib import Path
from .extraction import OUTPUT_SCHEMA
from .automation_provider import configured_models, CompatibleAutomaticModel, ProviderFailure

class ManualModel:
    def __init__(self, data_dir):
        self.transport, _ = configured_models(Path(data_dir)/"extraction-provider.json")
    def describe(self):
        return self.transport.describe()
    def model_digest(self, model_id):
        # Configuration identity only; cloud providers do not expose weight hashes.
        if not self.transport.available():
            raise ProviderFailure("MODEL_AUTH_MISSING")
        return hashlib.sha256(json.dumps(self.describe(),sort_keys=True).encode()).hexdigest()
    def generate(self, selected_text, repair=None):
        if not isinstance(self.transport, CompatibleAutomaticModel):
            raise ProviderFailure("MODEL_CONFIG_INVALID")
        prompt=("仅从原文提取最多5条记忆候选，不添加事实；quote逐字引用连续原文。"
                "明确最终决定才标Reported Decision，其余方案标Inferred OpenQuestion。"
                "Decision需decision_key，其余为null；不得自行批准。只返回JSON。\n原文：\n"+selected_text)
        if repair: prompt += "\n前次校验错误："+repair[:300]
        return self.transport._chat(prompt,OUTPUT_SCHEMA)[0]
