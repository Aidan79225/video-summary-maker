"""議案取證：用實測的無人載具條例草案資料。"""
from __future__ import annotations

from datetime import date

from factcheck.domain.entities import ClaimKind, ExtractedClaim, SourceKind
from factcheck.infrastructure.bill_source import BillSource, proposer_matches, term_on
from factcheck.infrastructure.lyapi import LyApi

from .lyapi_fakes import FakeFetch, load

SPEECH_DAY = date(2026, 8, 27)


def _claim(proposer="行政院", kind=ClaimKind.BILL_CONTENT, keywords="無人載具", law=""):
    return ExtractedClaim(quote="行政院提出的案子 6年2100億", kind=kind,
                          statement="行政院版本 6 年編列 2100 億", figures=("6年2100億",),
                          bill_keywords=keywords, proposer=proposer, law=law)


def _source(search=None):
    fetch = FakeFetch({
        "/bills": search or load("bills_search_無人載具.json"),
        "/bills/201110221870000": load("bill_201110221870000.json"),
        "/bills/202110223690000": load("bill_202110223690000.json"),
    })
    return BillSource(LyApi(fetch=fetch)), fetch


def test_term_is_derived_from_the_speech_date():
    assert term_on(date(2026, 8, 27)) == 11
    assert term_on(date(2024, 2, 1)) == 11
    assert term_on(date(2024, 1, 31)) == 10


def test_party_names_match_their_caucus():
    assert proposer_matches("國民黨", "本院國民黨黨團")
    assert proposer_matches("國民黨黨團", "本院國民黨黨團")
    assert proposer_matches("行政院", "行政院")
    assert not proposer_matches("行政院", "本院國民黨黨團")
    assert proposer_matches("", "任何人")


def test_the_executive_yuan_version_carries_its_budget_article():
    evidence = _source()[0].find(_claim(), SPEECH_DAY)
    assert len(evidence) == 1
    item = evidence[0]
    assert item.source == SourceKind.BILL
    assert "二千一百億元" in item.excerpt
    assert item.official_url == "https://ppg.ly.gov.tw/ppg/bills/201110221870000/details"
    assert item.api_url == "https://ly.govapi.tw/v2/bills/201110221870000"
    assert item.title.startswith("行政院｜")


def test_a_party_name_finds_the_caucus_bill():
    evidence = _source()[0].find(_claim(proposer="國民黨"), SPEECH_DAY)
    assert "二千四百億元" in evidence[0].excerpt


def test_review_reports_are_skipped():
    """審查報告把好幾個版本併在一起，分不出是誰的主張。"""
    rows = load("bills_search_無人載具.json")["bills"]
    review = next(r for r in rows if r["提案來源"] == "審查報告")
    executive = next(r for r in rows if r["議案編號"] == "201110221870000")
    source, fetch = _source({"bills": [review, executive]})
    evidence = source.find(_claim(), SPEECH_DAY)
    assert len(evidence) == 1
    assert not any(review["議案編號"] in u for u in fetch.urls)


def test_bill_content_without_a_proposer_returns_no_evidence():
    """實測假陽性：王正旭沒講是誰的版本，卻抓到陳素月、陳培瑜等不相干版本裡剛好
    符合的數字，被自動判「相符」發佈。議案版本一定要能對上提案者，對不上就等於
    查無此案，不能拿別人版本的數字硬湊。"""
    source, fetch = _source()
    evidence = source.find(_claim(proposer=""), SPEECH_DAY)
    assert evidence == []
    assert fetch.urls == []


def test_bill_status_without_a_proposer_still_returns_evidence():
    """議案進度問的是「這個議案」的狀態，不是哪個版本，proposer 可以是空的。"""
    rows = load("bills_search_無人載具.json")["bills"]
    executive = next(r for r in rows if r["議案編號"] == "201110221870000")
    source, _fetch = _source({"bills": [executive]})
    evidence = source.find(_claim(kind=ClaimKind.BILL_STATUS, proposer=""), SPEECH_DAY)
    assert evidence


def test_bills_proposed_after_the_speech_are_not_evidence():
    """行政院版是 2026-06-26 提案，六月初的發言不可能在講它。"""
    assert _source()[0].find(_claim(), date(2026, 6, 1)) == []


def test_status_claims_quote_the_bill_status():
    evidence = _source()[0].find(_claim(kind=ClaimKind.BILL_STATUS), SPEECH_DAY)
    # 狀態是查詢當下的，不是發言當天的：標明白，判讀時才不會當成發言當時的狀態
    assert evidence[0].excerpt.startswith("查詢時的議案狀態：")


def test_keywords_are_tried_in_turn_until_something_is_found():
    """模型常給口語的「無人機」，議案名稱寫的是「無人載具」。"""
    full = load("bills_search_無人載具.json")

    def search(query):
        return full if query["q"] == '"無人載具"' else {"bills": []}

    evidence = _source(search)[0].find(_claim(keywords="無人機 無人載具"), SPEECH_DAY)
    assert evidence


def test_no_keywords_means_no_search():
    source, fetch = _source()
    assert source.find(_claim(keywords=""), SPEECH_DAY) == []
    assert fetch.urls == []


def _recording_search():
    queries: list[str] = []

    def search(query):
        queries.append(query["q"])
        return {"bills": []}

    return search, queries


def test_the_proposer_is_never_used_as_a_keyword():
    """攔的 bug（實測）：模型把提案者塞進 bill_keywords，搜「行政院」會命中幾百個不相干的議案。"""
    search, queries = _recording_search()
    _source(search)[0].find(_claim(keywords="無人機 行政院", proposer="行政院"), SPEECH_DAY)
    assert queries == ['"無人機"']


def test_party_names_are_never_used_as_keywords():
    """實測抽出「醫療暴力 臺灣民眾黨」：政黨、黨團與它們的別名都不是議案名稱裡的詞。"""
    search, queries = _recording_search()
    claim = _claim(keywords="醫療暴力 臺灣民眾黨 國民黨黨團 民進黨 行政院", proposer="莊瑞雄")
    _source(search)[0].find(claim, SPEECH_DAY)
    assert queries == ['"醫療暴力"']


def test_bill_type_boilerplate_is_stripped_into_a_shorter_keyword():
    """實測：模型抽出「醫療法部分條文修正案」，但議案名稱裡精確比對查無此名——
    議案名稱其實只寫法規本體「醫療法」，「部分條文修正案」是泛用的法案格式詞。
    原詞先試、試不到再退到去掉格式詞的短詞。"""
    search, queries = _recording_search()
    claim = _claim(keywords="醫療法部分條文修正案", proposer="莊瑞雄")
    _source(search)[0].find(claim, SPEECH_DAY)
    assert queries == ['"醫療法部分條文修正案"', '"醫療法"']


def test_industry_development_boilerplate_is_also_stripped():
    """實測：模型抽出「無人機產業發展」，議案名稱其實只有「無人機」或「無人載具」。"""
    search, queries = _recording_search()
    claim = _claim(keywords="無人機產業發展", proposer="莊瑞雄")
    _source(search)[0].find(claim, SPEECH_DAY)
    assert queries == ['"無人機產業發展"', '"無人機"']


def test_the_claims_law_field_is_tried_after_the_keywords():
    """關鍵詞被過濾光時（例如模型只塞了提案者），至少還有 law 欄位可以退回去搜。"""
    search, queries = _recording_search()
    claim = _claim(keywords="行政院", proposer="行政院", law="醫療法")
    _source(search)[0].find(claim, SPEECH_DAY)
    assert queries == ['"醫療法"']


def test_無人機_falls_back_through_the_shortened_keyword_to_the_real_bill():
    """對真實案例的回歸測試：模型抽出「無人機產業發展」，LYAPI 的 q 是精確比對，
    用會依查詢字串分流的假路由模擬「只有『無人機』查得到、原詞查不到」。"""
    full = load("bills_search_無人載具.json")

    def search(query):
        return full if query["q"] == '"無人機"' else {"bills": []}

    claim = _claim(keywords="無人機產業發展", proposer="行政院")
    evidence = _source(search)[0].find(claim, SPEECH_DAY)
    assert "二千一百億元" in evidence[0].excerpt


def test_traditional_and_simplified_tai_are_treated_as_the_same_caucus_name():
    """LYAPI 的提案單位欄位寫「本院台灣民眾黨黨團」（台），模型常寫「臺灣民眾黨」（臺）。"""
    assert proposer_matches("臺灣民眾黨", "本院台灣民眾黨黨團")


def test_national_party_still_matches_after_the_tai_normalization_change():
    assert proposer_matches("國民黨", "本院國民黨黨團")


def test_the_executive_yuan_still_does_not_match_a_caucus_field():
    assert not proposer_matches("行政院", "本院國民黨黨團")


def test_a_party_not_in_the_alias_table_matches_its_caucus_field():
    """別名表只列了常見的幾個政黨；別的政黨只要欄位是「政黨名稱+團」也該比對得到。"""
    assert proposer_matches("綠黨", "本院綠黨團")


def test_the_next_keyword_is_tried_when_the_first_has_no_matching_candidate():
    """實測：q="無人機產業發展" 查到 5 筆提案者對不上的不相干議案——查有結果不代表
    有候選，一樣要往下一個（短化後的）關鍵詞試，才找得到行政院版。"""
    full = load("bills_search_無人載具.json")
    unrelated = {"bills": [
        {"議案編號": f"9999999999999990{i}", "提案單位/提案委員": f"本院委員某某{i}等16人",
         "提案來源": "委員提案"}
        for i in range(5)
    ]}

    def search(query):
        if query["q"] == '"無人機產業發展"':
            return unrelated
        if query["q"] == '"無人機"':
            return full
        return {"bills": []}

    claim = _claim(keywords="無人機產業發展", proposer="行政院")
    evidence = _source(search)[0].find(claim, SPEECH_DAY)
    assert "二千一百億元" in evidence[0].excerpt


def test_shortened_keywords_are_also_excluded_when_they_become_the_proposer():
    """短化後可能剛好變成提案者本人（例如「行政院修正案」剝到「行政院」）：
    跟原詞一樣，不能拿去搜。"""
    search, queries = _recording_search()
    claim = _claim(keywords="行政院修正案", proposer="行政院")
    _source(search)[0].find(claim, SPEECH_DAY)
    assert queries == ['"行政院修正案"']
