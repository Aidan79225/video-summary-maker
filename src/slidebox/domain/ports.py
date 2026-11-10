"""領域介面（port）：由 infrastructure 實作，usecases 依賴這些抽象。"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Callable, Protocol

from .entities import (
    AudioClip,
    Brief,
    Cue,
    Deck,
    DiarizerOutput,
    FollowUpPair,
    FollowUpResult,
    RawSlide,
    Slide,
    TopicLabel,
    TopicResult,
    Transcript,
    Word,
    WordTranscript,
)

# 進度回報：fraction 為 0..1，None 表示不確定；status 為文字說明。
ProgressCallback = Callable[[float | None, str], None]

# 取消判斷：回傳 True 表示使用者已要求取消。
CancelCheck = Callable[[], bool]


class SubtitleGateway(Protocol):
    def fetch(self, url: str, langs: Sequence[str]) -> Transcript:
        """抓字幕並解析。

        拿不到可用字幕時 raise NoSubtitlesAvailable——包含影片根本沒有字幕、
        以及字幕存在但下載失敗（例如 HTTP 429）。對呼叫端而言兩者結局相同：
        這部影片的字幕無法取得，該改走其他來源（例如語音辨識）。
        """
        ...


class Summarizer(Protocol):
    def summarize(
        self,
        compressed: str,
        duration: float,
        min_slides: int,
        max_slides: int,
        hint: str,
        progress: ProgressCallback,
        detailed: bool = False,
        is_cancelled: CancelCheck | None = None,
    ) -> tuple[Slide, ...]:
        """回傳 image_path 皆為 None 的 Slide 序列。

        compressed 是 compress_cues() 的產出——壓縮是純邏輯，不是
        summarizer 的責任。hint 為空字串表示首次嘗試，非空時是上一次
        的驗證錯誤，會附進提示裡要求模型修正。

        detailed 為 True 時另外要求每頁一段完整敘述，填進 Slide.detail。

        is_cancelled 要在生成途中反覆檢查並 raise OperationCancelled：這是整條
        pipeline 最久的一步（詳細模式可達數分鐘），只在開始前檢查一次等於
        不能取消。
        """
        ...


class BriefWriter(Protocol):
    def write(
        self,
        slides: Sequence[Slide],
        progress: ProgressCallback,
        is_cancelled: CancelCheck | None = None,
        hint: str = "",
    ) -> Brief:
        """從已經驗證過的分段摘要寫出質詢卡。hint 同 Summarizer：重試時的錯誤回饋。

        輸入是分段摘要而不是整份字幕：分段已經把內容濃縮到幾千字，再餵一次
        整份字幕是把最貴的那一步做兩遍。輸出不做落地檢查——那是純邏輯
        （usecases.brief.ground_brief）的責任，這裡只負責拿到模型的回應。

        回應不是合法的結構時 raise SummarizerOutputInvalid；連不上模型時
        raise SummarizerUnavailable。取消的規則與 Summarizer 相同。
        """
        ...


class SlideRewriter(Protocol):
    def rewrite(
        self,
        slide: Slide,
        problem: str,
        progress: ProgressCallback,
        is_cancelled: CancelCheck | None = None,
    ) -> Slide:
        """把一頁的條列與敘述改寫成第三人稱，其餘欄位（index、title、timestamp）原樣保留。

        整份重新產生修不好第一人稱：模型修好這一頁，別頁又冒出「我們」。
        只送有問題的那一頁回去改，範圍小、會收斂。problem 是驗證器的描述，
        含被抓到的詞與前後文。

        回應不是合法的結構時 raise SummarizerOutputInvalid；連不上模型時
        raise SummarizerUnavailable。取消的規則與 Summarizer 相同。
        """
        ...


class TopicClassifier(Protocol):
    def classify(
        self,
        text: str,
        labels: Sequence[TopicLabel],
        progress: ProgressCallback,
        is_cancelled: CancelCheck | None = None,
    ) -> TopicResult:
        """把一段文字分到 labels 裡的一個主領域（與可有可無的次領域）。

        回應解析不出來、或主領域不在 labels 裡時 raise SummarizerOutputInvalid
        ——不猜「最接近的」，那等於替模型做決定；連不上模型時 raise
        SummarizerUnavailable。取消的規則與 Summarizer 相同。
        """
        ...


class FollowUpJudge(Protocol):
    def judge(
        self,
        pair: FollowUpPair,
        progress: ProgressCallback,
        is_cancelled: CancelCheck | None = None,
    ) -> FollowUpResult:
        """判斷後來那篇有沒有再提先前那項要求的同一件事，有的話附一句引用。

        回應解析不出來、或判斷不是布林值時 raise SummarizerOutputInvalid——不把
        "true"、1 解讀成有追問；連不上模型時 raise SummarizerUnavailable。引用
        有沒有落地不在這裡檢查，那是新聞服務的事。取消的規則與 Summarizer 相同。
        """
        ...


class VideoSectionGateway(Protocol):
    def download_sections(
        self,
        url: str,
        timestamps: Sequence[float],
        max_height: int | None,
        dest_dir: str,
        progress: ProgressCallback,
        is_cancelled: CancelCheck,
    ) -> list[str | None]:
        """每個時間點回傳一個本地片段檔路徑；該點失敗則該位置為 None。

        自行建立 dest_dir。回傳長度必須等於 timestamps 長度。有失敗時回傳 SectionClips
        並帶上第一個失敗原因，完成訊息才寫得出為什麼沒有截圖。
        """
        ...

    def cleanup(self, dest_dir: str) -> None:
        """刪除暫存片段目錄。由 use case 在 finally 裡呼叫。

        **必須容忍目錄不存在，且絕不可 raise。** 取消發生在摘要階段時，
        這個目錄根本還沒被建立；此時若拋出例外，會從 finally 取代掉正在
        傳播的 OperationCancelled，讓使用者的「取消」變成檔案系統錯誤。
        """
        ...


class FrameExtractor(Protocol):
    def extract(self, section_path: str, dest_path: str, width: int) -> None:
        """取片段中間那一格，縮到 width，存成 WebP。失敗時 raise。

        自行建立 dest_path 的上層目錄——use case 只做路徑字串運算。
        """
        ...


class DeckRenderer(Protocol):
    def render(self, deck: Deck, dest_path: str) -> None:
        """把 deck 寫成單一 HTML 檔。自行建立上層目錄。"""
        ...


class ModelCatalog(Protocol):
    def list_models(self) -> list[str]:
        """列出本機可用模型，供 UI 下拉。連不上時回傳空陣列，不 raise。"""
        ...


class AudioGateway(Protocol):
    def download_audio(
        self,
        url: str,
        dest_dir: str,
        progress: ProgressCallback,
        is_cancelled: CancelCheck,
    ) -> AudioClip:
        """下載影片的音訊到 dest_dir（自行建立），連同影片資訊回傳。

        失敗時 raise NoSubtitlesAvailable——走到這一步表示影片已經沒有字幕，
        音訊再拿不到，對呼叫端而言結局相同。
        """
        ...

    def cleanup(self, dest_dir: str) -> None:
        """刪除暫存音訊目錄。同 VideoSectionGateway.cleanup：必須容忍目錄
        不存在，且絕不可 raise——它在 finally 裡被呼叫。"""
        ...


class SpeechTranscriber(Protocol):
    def transcribe(
        self,
        audio_path: str,
        duration: float,
        progress: ProgressCallback,
        is_cancelled: CancelCheck,
        initial_prompt: str | None = None,
    ) -> tuple[tuple[Cue, ...], str]:
        """語音辨識，回傳 (字幕, 偵測到的語言代碼)。

        initial_prompt 是給辨識器的提示：講者姓名、機關名這類專有名詞，
        模型會優先用這些寫法（「楊啓邦」而不是同音的「楊啟邦」）。

        沒偵測到任何語音時回傳空的字幕 tuple，由呼叫端決定如何告知使用者。
        逐段檢查 is_cancelled，取消時 raise OperationCancelled。
        duration 只用於進度顯示，給 0 時不顯示總長。
        """
        ...


class WordTranscriber(Protocol):
    """多人模式的語音辨識：每個字都帶時間，才能把字對到說話者。

    跟 SpeechTranscriber 是兩個介面——單人路徑的 transcribe() 與它的呼叫一個字都不動。
    """

    def transcribe_words(
        self,
        audio_path: str,
        duration: float,
        progress: ProgressCallback,
        is_cancelled: CancelCheck,
        speech_hint: str | None = None,
    ) -> tuple[tuple[Word, ...], str]:
        """回傳 (按時間排序的字, 偵測到的語言代碼)。

        speech_hint 的意義同 SpeechTranscriber.transcribe 的 initial_prompt。沒偵測到
        任何語音時回傳空的 tuple。逐段檢查 is_cancelled，取消時 raise OperationCancelled。
        """
        ...


class SpeechDiarizer(Protocol):
    """把音訊切成說話者區段（diarization）。引擎可以換，介面不變。"""

    @property
    def engine(self) -> str:
        """引擎名稱，例如 "pyannote.audio"。"""
        ...

    @property
    def version(self) -> str:
        """引擎套件的版本；沒安裝時是空字串。"""
        ...

    @property
    def model(self) -> str:
        """模型識別，例如 "pyannote/speaker-diarization-community-1"。"""
        ...

    @property
    def fingerprint(self) -> str:
        """引擎＋版本＋模型的指紋（16 個十六進位字元）：存檔與結果都帶著它，
        換了引擎或模型，舊的說話者區分結果就不會被拿來冒充。"""
        ...

    def available(self) -> tuple[bool, str]:
        """回傳 (可用與否, 給管理者看的原因)。**要便宜**：不載入模型、不下載任何東西。"""
        ...

    def diarize(
        self,
        audio_path: str,
        duration: float,
        speaker_count: int | None,
        progress: ProgressCallback,
        is_cancelled: CancelCheck,
    ) -> DiarizerOutput:
        """回傳互斥的說話者區段（按時間排序）與重疊秒數；speaker 是引擎自己的原始標籤。

        speaker_count 為 None 表示自動判斷人數，否則是固定的人數（>= 2）。
        任何失敗都 raise DiarizationUnavailable（訊息給操作者看）；取消時 raise
        OperationCancelled。
        """
        ...


class ConversationSummarizer(Protocol):
    """把一段已標好說話者的對話寫成「重點」：模型只引用 turn 的編號，不決定誰說了什麼。"""

    def summarize_window(
        self,
        window_text: str,
        first_turn: int,
        last_turn: int,
        min_slides: int,
        max_slides: int,
        hint: str,
        progress: ProgressCallback,
        is_cancelled: CancelCheck | None = None,
    ) -> tuple[RawSlide, ...]:
        """window_text 是 format_window() 的產出（`[T12] S1：…` 一行一段話）。

        回傳的重點只能引用 first_turn..last_turn 之間的編號；超出的由程式檢查丟掉，
        這裡不必信任也不必過濾。hint 非空時是上一次被程式檢查退回的原因。

        回應不是合法的結構時 raise SummarizerOutputInvalid；連不上模型時 raise
        SummarizerUnavailable。取消的規則與 Summarizer 相同。
        """
        ...


class Checkpoints(Protocol):
    """做完的貴步驟（逐字稿、摘要、摘要卡）存起來：工作中途失敗或取消時，同一個網址再跑一次
    就從斷掉的地方接著做，不用再等一次語音辨識與摘要。

    fingerprint 是產生那一步時的設定（模型、頁數範圍…）；讀的時候對不上就當作沒有，不拿舊設定
    的結果冒充。**存、讀、清都不得 raise**：存檔是加值，工作不能因為它失敗。
    """

    def load_transcript(self, output_dir: str, url: str,
                        fingerprint: str) -> tuple[Transcript, str] | None:
        """回傳 (逐字稿, 來源註記)；沒有或對不上時回 None。"""
        ...

    def save_transcript(self, output_dir: str, url: str, fingerprint: str,
                        transcript: Transcript, note: str) -> None:
        ...

    def load_slides(self, output_dir: str, url: str,
                    fingerprint: str) -> tuple[tuple[Slide, ...], str] | None:
        """回傳 (投影片, 改不掉的第一人稱註記)；沒有或對不上時回 None。"""
        ...

    def save_slides(self, output_dir: str, url: str, fingerprint: str,
                    slides: Sequence[Slide], note: str) -> None:
        ...

    def load_brief(self, output_dir: str, url: str, fingerprint: str) -> Brief | None:
        ...

    def save_brief(self, output_dir: str, url: str, fingerprint: str, brief: Brief) -> None:
        ...

    # 以下兩個階段只有多人模式用：單人模式的階段（transcript、slides、brief）與檔案不變。
    # 多人模式的 diarization 是 CPU 上最貴的一步、words 是語音辨識，各自存檔，
    # 一邊失敗或被取消時另一邊不必重做。**絕不存降級或沒有說話者的結果。**

    def load_diarization(self, output_dir: str, url: str,
                         fingerprint: str) -> DiarizerOutput | None:
        ...

    def save_diarization(self, output_dir: str, url: str, fingerprint: str,
                         output: DiarizerOutput) -> None:
        ...

    def load_words(self, output_dir: str, url: str,
                   fingerprint: str) -> WordTranscript | None:
        ...

    def save_words(self, output_dir: str, url: str, fingerprint: str,
                   words: WordTranscript) -> None:
        ...

    def clear(self, output_dir: str, url: str) -> None:
        """整份成功出片後刪掉這個網址的存檔。"""
        ...
