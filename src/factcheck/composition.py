"""組裝：把 LYAPI、法條／議案來源與 Ollama 接到 use case 上。

換模型或換資料來源只動這裡。
"""
from __future__ import annotations

from .domain.entities import ClaimKind
from .infrastructure.bill_source import BillSource
from .infrastructure.law_source import LawSource
from .infrastructure.lyapi import DEFAULT_BASE, LyApi
from .infrastructure.ollama import OllamaClaimExtractor, OllamaTextJudge
from .usecases.check import FactCheckUseCase


def build_factcheck(ollama_host: str, model: str, num_ctx: int,
                    lyapi_base: str = DEFAULT_BASE) -> FactCheckUseCase:
    api = LyApi(lyapi_base)
    bills = BillSource(api)
    return FactCheckUseCase(
        extractor=OllamaClaimExtractor(ollama_host, model, num_ctx),
        sources={
            ClaimKind.LAW_ARTICLE: LawSource(api),
            ClaimKind.BILL_CONTENT: bills,
            ClaimKind.BILL_STATUS: bills,
        },
        judge=OllamaTextJudge(ollama_host, model, num_ctx),
    )
