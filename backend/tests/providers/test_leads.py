from __future__ import annotations

from kerui_recruit.bd_search.service import LeadInfo
from kerui_recruit.providers.leads import DeepSeekLeadExtractor


class FakeLlm:
    def __init__(self, parsed) -> None:
        self.parsed = parsed

    async def complete_json(self, messages, response_model, temperature=None, **kwargs):
        return self.parsed


class ErrorLlm:
    async def complete_json(self, messages, response_model, temperature=None, **kwargs):
        raise RuntimeError("boom")


class ParsedLead:
    def __init__(self, company, job_title):
        self.company = company
        self.job_title = job_title


def test_deepseek_extractor_maps_company_and_job() -> None:
    from kerui_recruit.providers.leads import ParsedLead as PL

    llm = FakeLlm(PL(company="字节跳动科技有限公司", job_title="高级 Java 工程师"))
    extractor = DeepSeekLeadExtractor(llm)

    info = extractor.extract("字节跳动科技有限公司 — 高级 Java 工程师", "招聘高级 Java 工程师")

    assert info == LeadInfo(company="字节跳动科技有限公司", job_title="高级 Java 工程师")


def test_deepseek_extractor_falls_back_on_error() -> None:
    extractor = DeepSeekLeadExtractor(ErrorLlm())

    info = extractor.extract("腾讯科技（深圳）有限公司 前端开发工程师", "招聘前端开发工程师")

    assert info.company is not None
    assert "腾讯" in info.company


def test_deepseek_extractor_falls_back_on_empty_result() -> None:
    from kerui_recruit.providers.leads import ParsedLead as PL

    extractor = DeepSeekLeadExtractor(FakeLlm(PL(company=None, job_title=None)))

    info = extractor.extract("阿里云计算有限公司 招聘 Java 高级工程师", "阿里云计算有限公司")

    assert info.company is not None
    assert "阿里" in info.company
