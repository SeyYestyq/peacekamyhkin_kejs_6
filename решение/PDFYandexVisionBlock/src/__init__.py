from __future__ import annotations

import base64
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# PyPDF2 и requests импортируются лениво (внутри функций),
# чтобы расширение устанавливалось в Студию без ошибок,
# даже если пакеты ещё не скачаны. Студия доустановит их при первом запуске.
requests = None  # type: Any
PyPDF2 = None    # type: Any
PdfReader = None # type: Any

def _ensure_dependencies():
    """Ленивый импорт зависимостей при первом вызове."""
    global requests, PyPDF2, PdfReader
    if requests is None:
        import requests as _requests
        requests = _requests
    if PyPDF2 is None:
        import PyPDF2 as _PyPDF2
        PyPDF2 = _PyPDF2
        PdfReader = _PyPDF2.PdfReader

def _show_error_message(title: str, message: str) -> None:
    try:
        import sys
        if sys.platform == "darwin":
            import subprocess
            # Escape quotes for AppleScript
            safe_msg = message.replace('"', '\\"')
            safe_title = title.replace('"', '\\"')
            script = f'display dialog "{safe_msg}" with title "{safe_title}" buttons {{"OK"}} default button "OK" with icon stop'
            subprocess.run(["osascript", "-e", script], check=False)
        elif sys.platform == "win32":
            import ctypes
            # MB_ICONERROR = 0x10, MB_SYSTEMMODAL = 0x1000
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x1010)
        else:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            messagebox.showerror(title, message, parent=root)
            root.destroy()
    except Exception:
        pass

try:
    from puzzle_logger import log_decorator, window_logger
except Exception:
    def _identity_decorator(func=None, *args, **kwargs):
        if func is None:
            def wrapper(inner_func):
                return inner_func
            return wrapper
        return func
    log_decorator = _identity_decorator
    window_logger = _identity_decorator


VISION_ENDPOINT = "https://vision.api.cloud.yandex.net/vision/v1/batchAnalyze"
IAM_TOKEN_ENDPOINT = "https://iam.api.cloud.yandex.net/iam/v1/tokens"
PRIMARY_FEATURE_TYPE = "DOCUMENT_RECOGNITION"
FALLBACK_FEATURE_TYPE = "TEXT_DETECTION"

DOC_TYPES = [
    ("Платёжное поручение", re.compile(r"плат[её]жн[ое]*\s*поручен", re.IGNORECASE)),
    ("Счёт-фактура", re.compile(r"сч[её]т[- ]фактур", re.IGNORECASE)),
    ("Накладная", re.compile(r"накладн", re.IGNORECASE)),
    ("Акт", re.compile(r"\b[АA]кт\b", re.IGNORECASE)),
    ("Договор", re.compile(r"\bдоговор\b", re.IGNORECASE)),
    ("Контракт", re.compile(r"\bконтракт\b", re.IGNORECASE)),
    ("Счёт на оплату", re.compile(r"сч[её]т\s+(?:№|на\s+оплату)", re.IGNORECASE)),
    ("УПД", re.compile(r"\bУПД\b", re.IGNORECASE)),
]

ROLE_PATTERNS = {
    "supplier": re.compile(
        r"(?:поставщик|продавец|исполнитель|подрядчик|получатель)[:\-\s]*[\n\r]*([^\n]+)",
        re.IGNORECASE,
    ),
    "buyer": re.compile(
        r"(?:покупатель|заказчик|плательщик)[:\-\s]*[\n\r]*([^\n]+)",
        re.IGNORECASE,
    ),
}

INN_PATTERN = re.compile(r"ИНН\s*[:№]?\s*(\d{10,12})", re.IGNORECASE)
KPP_PATTERN = re.compile(r"КПП\s*[:№]?\s*(\d{9})(?!\d)", re.IGNORECASE)

PAYMENT_ORDER_NUM_PATTERN = re.compile(
    r"(?:плат[её]жн[ое]*\s*поручен(?:ие|ия|ий)?)[^\n]{0,60}?[№N]\s*(\d[\w\-/]*)",
    re.IGNORECASE,
)

PAYMENT_NUM_PATTERN = re.compile(
    r"(?:№|N|No\.?|номер)\s*платежа\s*[:№]?\s*(\d[\w\-/]*)",
    re.IGNORECASE,
)

BASIS_PATTERNS = [
    re.compile(
        r"(?:основание|основание\s*платежа)[:\-\s]*[\n\r]*([^\n]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:сч[её]т[- ]?фактур[аы]?|сч/ф)[^\n]{0,80}",
        re.IGNORECASE,
    ),
]

PURPOSE_PATTERNS = [
    re.compile(
        r"(оплата\s+(?:за|по)\s+[^\n]{3,120})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:назначение\s*платежа|назначение)[:\-\s]*[\n\r]*([^\n]+)",
        re.IGNORECASE,
    ),
]

DATE_PATTERNS = [
    re.compile(r"(\d{2}[./-]\d{2}[./-]\d{4})"),
]

AMOUNT_PATTERNS = [
    re.compile(
        r"(?:сумма\s*(?:платежа)?|итого|всего|к\s*оплате)[:\-\s]*[\n\r]*([1-9]\d{0,2}(?:\s\d{3})*[,.\-]\d{2}|0[,.\-]\d{2})\s*(?:руб|₽|р\.)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\d)([1-9]\d{0,2}(?:[ \xA0]\d{3})*[,.\-]\d{2}|\d+[,.\-]\d{2})(?!\d)\s*(?:руб|₽|р\.)?",
        re.IGNORECASE,
    ),
]

VAT_PATTERNS = [
    # В т.ч. НДС 20%
    re.compile(r"(?:в\s*т\.ч\.|в\s*том\s*числе)\s*ндс\s*(?:20%|18%|10%|0%)?\s*[:]?\s*([\d\s.,]+)\s*(?:руб|₽|р\.)?", re.IGNORECASE),
    # НДС 20% - 1500.00 руб
    re.compile(r"ндс\s+(?:20%|18%|10%|0%)\s*[-–]?\s*([\d\s.,]+)\s*(?:руб|₽|р\.)?", re.IGNORECASE),
    # Сумма НДС: 1500.00
    re.compile(r"(?:сумма\s*)?ндс\s*[:]?\s*([\d\s.,]+)\s*(?:руб|₽|р\.)?", re.IGNORECASE),
    # НДС по ставке 20% составляет
    re.compile(r"ндс\s*(?:по\s*ставке\s*)?(?:20%|18%|10%|0%)\s*(?:составляет\s*)?([\d\s.,]+)", re.IGNORECASE),
]

DISCOUNT_PATTERNS = [
    re.compile(r"(?:скидка|скидки|скидок)\s*[:]?\s*([\d\s.,]+)\s*(?:руб|₽|р\.)?", re.IGNORECASE),
    re.compile(r"(?:скидка|скидки)\s*(?:на\s*)?([\d\s.,]+)\s*(?:руб|₽|р\.)?", re.IGNORECASE),
]

AMOUNT_TEXT_PATTERN = re.compile(
    r"(?:сумма|итого|всего)[^\n]{0,300}?[«\"(]([^«\"()]{10,200})[»\")]",
    re.IGNORECASE,
)

ITEM_LINE_PATTERN = re.compile(
    r"^(?P<name>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<price>\d+(?:[.,]\d{2})?)\s+(?P<sum>\d+(?:[.,]\d{2})?)$"
)

# Паттерны для банковских реквизитов
BANK_PATTERNS = {
    "bik": re.compile(r"\b(0\d{8}|1\d{8})\b"),
    "account": re.compile(r"\b(\d{20})\b"),
    "corr_account": re.compile(r"\b(401028\d{13}|402028\d{13})\b"),
}

# Паттерны для извлечения дополнительных данных из назначения платежа
INVOICE_NUM_PATTERN = re.compile(r"(?:счет[а]?-фактур[аы]?|сч/ф|фактура)\s*(?:№|N)?\s*(\d+)", re.IGNORECASE)
INVOICE_DATE_PATTERN = re.compile(r"(?:счет[а]?-фактур[аы]?|сч/ф|фактура)[^\d]*(\d{2}[./-]\d{2}[./-]\d{4}|\d{1,2}\s+\w+\s+\d{4})", re.IGNORECASE)
CONTRACT_NUM_PATTERN = re.compile(r"(?:дог|договор|контракт)[^\d]*№?\s*(\d+[-\w]*)", re.IGNORECASE)
CONTRACT_DATE_PATTERN = re.compile(r"(?:дог|договор|контракт)[^\d]*от\s*(\d{2}[./-]\d{2}[./-]\d{4})", re.IGNORECASE)
AMOUNT_WORDS_PATTERN = re.compile(r"([А-Яа-яёЁ]+(?:надцать|дцать|сто|тысяч|рубл[ья]|копе(?:йка|ек))(?:\s+[А-Яа-яёЁ]+)*)", re.IGNORECASE)
OPERATION_TYPE_PATTERN = re.compile(r"(?:вид\s*операции|операция)\s*[:]?\s*(\d{2,3})", re.IGNORECASE)
QUEUE_PATTERN = re.compile(r"(?:очередность\s*платежа|очередь)\s*[:]?\s*(\d)", re.IGNORECASE)
UIN_PATTERN = re.compile(r"(?:уин|uin|код)\s*[:]?\s*(\d+)", re.IGNORECASE)


class DocumentRecognitionError(RuntimeError):
    pass


class YandexVisionAuthError(DocumentRecognitionError):
    pass


class YandexVisionQuotaError(DocumentRecognitionError):
    pass


class YandexVisionNetworkError(DocumentRecognitionError):
    pass


class YandexVisionResponseError(DocumentRecognitionError):
    pass


@dataclass(slots=True)
class ImportantData:
    номер: str | None = None
    дата: str | None = None
    inn_receiver: str | None = None
    inn_sender: str | None = None
    delivery: str | None = None
    amount_without_vat: str | None = None
    amount_with_vat: str | None = None
    vat_amount: str | None = None
    discounts: str | None = None
    исполнитель: str | None = None
    заказчик: str | None = None
    # Банковские реквизиты плательщика
    inn_sender_full: str | None = None
    kpp_sender: str | None = None
    account_sender: str | None = None
    bank_sender: str | None = None
    bik_sender: str | None = None
    corr_account_sender: str | None = None
    # Банковские реквизиты получателя
    inn_receiver_full: str | None = None
    kpp_receiver: str | None = None
    account_receiver: str | None = None
    bank_receiver: str | None = None
    bik_receiver: str | None = None
    corr_account_receiver: str | None = None
    # Дополнительные поля
    amount_words: str | None = None
    operation_type: str | None = None
    payment_queue: str | None = None
    uin: str | None = None
    # Парсинг назначения платежа
    invoice_number: str | None = None
    invoice_date: str | None = None
    contract_number: str | None = None
    contract_date: str | None = None

@dataclass(slots=True)
class ItemData:
    name: str = ""
    time_or_date: str = ""
    quantity: str = ""
    price: str = ""
    amount: str = ""
    short_description: str = ""

@dataclass(slots=True)
class DetailsData:
    items: list[ItemData] = field(default_factory=list)
    short_document_description: str = ""

@dataclass(slots=True)
class ExtractedDocument:
    important: ImportantData = field(default_factory=ImportantData)
    details: DetailsData = field(default_factory=DetailsData)
    document_type: str | None = None
    source_file: str | None = None
    text: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "важное": {
                "номер": self.important.номер,
                "дата": self.important.дата,
                "инн_получателя": self.important.inn_receiver,
                "инн_отправителя": self.important.inn_sender,
                "поставка": self.important.delivery,
                "сумма_без_ндс": self.important.amount_without_vat,
                "сумма_с_ндс": self.important.amount_with_vat,
                "сумма_ндс": self.important.vat_amount,
                "скидки": self.important.discounts,
                "исполнитель": self.important.исполнитель,
                "заказчик": self.important.заказчик,
                # Банковские реквизиты плательщика
                "инн_отправителя_полный": self.important.inn_sender_full,
                "кпп_отправителя": self.important.kpp_sender,
                "счет_отправителя": self.important.account_sender,
                "банк_отправителя": self.important.bank_sender,
                "бик_отправителя": self.important.bik_sender,
                "корр_счет_отправителя": self.important.corr_account_sender,
                # Банковские реквизиты получателя
                "инн_получателя_полный": self.important.inn_receiver_full,
                "кпп_получателя": self.important.kpp_receiver,
                "счет_получателя": self.important.account_receiver,
                "банк_получателя": self.important.bank_receiver,
                "бик_получателя": self.important.bik_receiver,
                "корр_счет_получателя": self.important.corr_account_receiver,
                # Дополнительные поля
                "сумма_прописью": self.important.amount_words,
                "вид_операции": self.important.operation_type,
                "очередность_платежа": self.important.payment_queue,
                "уин": self.important.uin,
                # Реквизиты из назначения платежа
                "номер_счета_фактуры": self.important.invoice_number,
                "дата_счета_фактуры": self.important.invoice_date,
                "номер_договора": self.important.contract_number,
                "дата_договора": self.important.contract_date,
            },
            "данные": {
                "товары": [
                    {
                        "название": item.name,
                        "время_или_дата": item.time_or_date,
                        "количество": item.quantity,
                        "цена": item.price,
                        "сумма": item.amount,
                        "краткое_описание": item.short_description,
                    } for item in self.details.items
                ],
                "краткое_описание_документа": self.details.short_document_description,
            },
            "метаданные": {
                "тип_документа": self.document_type,
                "номер": self.important.номер,
                "дата": self.important.дата,
                "исходный_файл": self.source_file,
                "предупреждения": self.warnings,
            }
        }


def _normalize_language(language: str | None) -> str:
    value = (language or "mixed").strip().lower()
    if value in {"ru", "русский", "russian"}:
        return "ru"
    if value in {"en", "английский", "english"}:
        return "en"
    return "mixed"


def _normalize_output_format(output_format: str | None) -> str:
    value = (output_format or "dict").strip().lower()
    return "json" if value == "json" else "dict"


def _read_pdf_base64(file_path: str) -> tuple[str, bytes]:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF-файл не найден: {file_path}")
    if not path.is_file():
        raise DocumentRecognitionError(f"Указанный путь не является файлом: {file_path}")

    pdf_bytes = path.read_bytes()
    encoded = base64.b64encode(pdf_bytes).decode("ascii")
    return encoded, pdf_bytes


def _extract_pdf_text_locally(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:
        return ""
    text_chunks: list[str] = []
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        if page_text.strip():
            text_chunks.append(page_text)
    return "\n".join(text_chunks).strip()


def _request_payload(folder_id: str, pdf_base64: str, language: str, feature_type: str) -> dict[str, Any]:
    feature: dict[str, Any] = {"type": feature_type}
    if feature_type == FALLBACK_FEATURE_TYPE:
        language_codes = [language] if language in {"ru", "en"} else ["ru", "en"]
        feature["textDetectionConfig"] = {"languageCodes": language_codes}

    return {
        "folderId": folder_id,
        "analyze_specs": [
            {
                "content": pdf_base64,
                "mimeType": "application/pdf",
                "features": [feature],
            }
        ],
    }


def _post_batch_analyze(token: str, folder_id: str, pdf_base64: str, language: str) -> dict[str, Any]:
    def _perform(feature_type: str, auth_scheme: str, auth_token: str) -> requests.Response:
        payload = _request_payload(folder_id, pdf_base64, language, feature_type)
        headers = {
            "Authorization": f"{auth_scheme} {auth_token}",
            "Content-Type": "application/json",
        }
        try:
            return requests.post(VISION_ENDPOINT, headers=headers, json=payload, timeout=120)
        except requests.exceptions.Timeout as exc:
            raise YandexVisionNetworkError("Превышено время ожидания ответа от Yandex Vision.") from exc
        except requests.exceptions.RequestException as exc:
            raise YandexVisionNetworkError(f"Сетевая ошибка при обращении к Yandex Vision: {exc}") from exc

    def _parse_response(response: requests.Response, feature_type: str) -> dict[str, Any]:
        if response.status_code in {401, 403}:
            raise YandexVisionAuthError("Неверный токен или недостаточно прав для доступа к Yandex Vision.")
        if response.status_code == 429:
            raise YandexVisionQuotaError("Превышена квота или лимит запросов Yandex Vision.")
        if response.status_code >= 500:
            raise YandexVisionNetworkError(
                f"Сервис Yandex Vision вернул ошибку {response.status_code}: {response.text[:500]}"
            )
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            raise YandexVisionResponseError(
                f"HTTP-ошибка Yandex Vision {response.status_code} при feature={feature_type}: {response.text[:500]}"
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise YandexVisionResponseError("Некорректный JSON-ответ от Yandex Vision.") from exc
        if not isinstance(data, dict):
            raise YandexVisionResponseError("Неверная структура ответа от Yandex Vision.")
        return data

    def _exchange_oauth_to_iam(oauth_token: str) -> str | None:
        payload = {"yandexPassportOauthToken": oauth_token}
        try:
            response = requests.post(IAM_TOKEN_ENDPOINT, json=payload, timeout=60)
        except requests.exceptions.RequestException:
            return None
        if response.status_code != 200:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        iam_token = data.get("iamToken")
        return iam_token if isinstance(iam_token, str) and iam_token.strip() else None

    for auth_scheme, auth_token in (("Api-Key", token), ("Bearer", token)):
        primary_response = _perform(PRIMARY_FEATURE_TYPE, auth_scheme, auth_token)
        if primary_response.status_code == 400 and "invalid value" in primary_response.text.lower():
            try:
                fallback_response = _perform(FALLBACK_FEATURE_TYPE, auth_scheme, auth_token)
                return _parse_response(fallback_response, FALLBACK_FEATURE_TYPE)
            except YandexVisionAuthError:
                if auth_scheme == "Bearer":
                    continue
                raise
        try:
            return _parse_response(primary_response, PRIMARY_FEATURE_TYPE)
        except YandexVisionAuthError:
            if auth_scheme == "Bearer":
                continue
            raise

    iam_token = _exchange_oauth_to_iam(token)
    if iam_token:
        for feature_type in (PRIMARY_FEATURE_TYPE, FALLBACK_FEATURE_TYPE):
            response = _perform(feature_type, "Bearer", iam_token)
            if response.status_code == 400 and "invalid value" in response.text.lower() and feature_type == PRIMARY_FEATURE_TYPE:
                continue
            return _parse_response(response, feature_type)

    raise YandexVisionAuthError("Неверный токен или недостаточно прав для доступа к Yandex Vision.")


def _walk_strings(node: Any) -> Iterable[str]:
    if isinstance(node, str):
        value = node.strip()
        if value:
            yield value
    elif isinstance(node, dict):
        for key, value in node.items():
            if key.lower() in {"text", "value", "content", "recognizedtext", "fulltext", "plaintext"}:
                if isinstance(value, str) and value.strip():
                    yield value.strip()
            yield from _walk_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_strings(item)


def _collect_tables(node: Any) -> list[Any]:
    tables: list[Any] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() in {"tables", "table", "tablerecognition", "recognizedtables"}:
                if isinstance(value, list):
                    tables.extend(value)
                else:
                    tables.append(value)
            tables.extend(_collect_tables(value))
    elif isinstance(node, list):
        for item in node:
            tables.extend(_collect_tables(item))
    return tables


def _extract_text_detection_lines(node: Any) -> list[str]:
    lines: list[str] = []
    if isinstance(node, dict):
        text_detection = node.get("textDetection")
        if isinstance(text_detection, dict):
            for page in text_detection.get("pages", []):
                if not isinstance(page, dict):
                    continue
                for block in page.get("blocks", []):
                    if not isinstance(block, dict):
                        continue
                    for line in block.get("lines", []):
                        if not isinstance(line, dict):
                            continue
                        words: list[str] = []
                        for word in line.get("words", []):
                            if isinstance(word, dict):
                                word_text = word.get("text")
                                if isinstance(word_text, str) and word_text.strip():
                                    words.append(word_text.strip())
                        if words:
                            lines.append(" ".join(words))
        for value in node.values():
            lines.extend(_extract_text_detection_lines(value))
    elif isinstance(node, list):
        for item in node:
            lines.extend(_extract_text_detection_lines(item))
    return lines


def _extract_full_text_from_document_recognition(node: Any) -> list[str]:
    """Извлекает полный текст из результата DOCUMENT_RECOGNITION (entities -> pages -> blocks -> lines)."""
    lines: list[str] = []
    if isinstance(node, dict):
        entities = node.get("entities")
        if isinstance(entities, list):
            for entity in entities:
                if not isinstance(entity, dict):
                    continue
                for page in entity.get("pages", []):
                    if not isinstance(page, dict):
                        continue
                    for block in page.get("blocks", []):
                        if not isinstance(block, dict):
                            continue
                        for line in block.get("lines", []):
                            if not isinstance(line, dict):
                                continue
                            words: list[str] = []
                            for word in line.get("words", []):
                                if isinstance(word, dict):
                                    word_text = word.get("text")
                                    if isinstance(word_text, str) and word_text.strip():
                                        words.append(word_text.strip())
                            if words:
                                lines.append(" ".join(words))
        for value in node.values():
            lines.extend(_extract_full_text_from_document_recognition(value))
    elif isinstance(node, list):
        for item in node:
            lines.extend(_extract_full_text_from_document_recognition(item))
    return lines


def _join_text_from_response(response_data: dict[str, Any]) -> str:
    doc_lines = _extract_full_text_from_document_recognition(response_data)
    if doc_lines:
        return "\n".join(dict.fromkeys(line.strip() for line in doc_lines if line.strip()))

    td_lines = _extract_text_detection_lines(response_data)
    if td_lines:
        return "\n".join(dict.fromkeys(line.strip() for line in td_lines if line.strip()))

    parts: list[str] = []
    for value in _walk_strings(response_data):
        if re.fullmatch(r"[\d\s.,\-]{1,20}", value):
            continue
        parts.append(value)
    return "\n".join(dict.fromkeys(parts))


def _find_document_type(text: str) -> str | None:
    for label, pattern in DOC_TYPES:
        if pattern.search(text):
            return label
    return None


def _find_document_date(text: str) -> str | None:
    date_matches = [(match.group(1), match.start()) for match in re.finditer(r"(\d{2}[./-]\d{2}[./-]\d{4})", text)]
    if not date_matches:
        return None

    if len(date_matches) == 1:
        return date_matches[0][0]

    date_keywords = [
        r"дата\s*:?\s*$",
        r"дата\s*платежа",
        r"дата\s*документа",
        r"плат[её]жн[ое]*\s*поручен",
    ]

    for kw_pattern in date_keywords:
        for date_str, pos in date_matches:
            context_start = max(0, pos - 200)
            context = text[context_start:pos]
            if re.search(kw_pattern, context, re.IGNORECASE):
                return date_str

    for date_str, pos in date_matches:
        context_start = max(0, pos - 120)
        context = text[context_start:pos + len(date_str) + 20]
        if re.search(r"(?:№|N|номер)\s*(?:платежа|плат|поруч)", context, re.IGNORECASE):
            return date_str

    return date_matches[-1][0]


def _normalize_amount(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.replace(" ", "").replace("\xa0", "").replace(",", ".").replace("-", ".")
    match = re.search(r"\d+(?:\.\d{1,2})?", cleaned)
    if match:
        # Всегда возвращаем сумму с 2 знаками после запятой
        try:
            amount = float(match.group(0))
            return f"{amount:.2f}"
        except ValueError:
            pass
    return value.strip() if value else None


def _format_amount_display(value: str | None) -> str | None:
    if not value:
        return None
    normalized = _normalize_amount(value)
    if not normalized:
        return None
    try:
        parts = normalized.split(".")
        integer_part = f"{int(parts[0]):,}".replace(",", " ")
        if len(parts) > 1:
            return f"{integer_part},{parts[1]} руб."
        return f"{integer_part} руб."
    except (ValueError, TypeError):
        return f"{normalized} руб."


def _find_total_amount(text: str) -> str | None:
    for pattern in AMOUNT_PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group(1).strip()
            # If it's literally just "DD.MM", it might be a date part
            if re.fullmatch(r"\d{2}[.,]\d{2}", raw):
                # Check surrounding context for year
                ctx = text[max(0, match.start() - 5):min(len(text), match.end() + 5)]
                if re.search(r"\d{4}", ctx):
                    continue
            normalized = _normalize_amount(raw)
            if normalized:
                return normalized
    return None


def _find_amount_text(text: str) -> str | None:
    match = AMOUNT_TEXT_PATTERN.search(text)
    if match:
        return match.group(1).strip()
    amount_line = re.search(
        r"(\d[\d\s.,\-]+\s*(?:руб|₽|р\.?).{0,200})",
        text,
        re.IGNORECASE,
    )
    if amount_line:
        text_match = re.search(
            r"[«\"(]([^«\"()]{8,200})[»\")]",
            amount_line.group(0),
        )
        if text_match:
            return text_match.group(1).strip()
    return None


def _is_date_component(value: str) -> bool:
    """Проверяет, не является ли значение частью даты (например, '30' из '30.11.2021')."""
    return bool(re.fullmatch(r"\d{1,2}", value))


def _find_payment_order_number(text: str) -> str | None:
    # First try: look for number in the tail portion of the document
    # (payment order number is typically at the bottom)
    date_matches = list(re.finditer(r"\d{2}[./-]\d{2}[./-]\d{4}", text))

    # Tail scan: after the last date, there's often the payment order number
    if date_matches:
        last_date_end = date_matches[-1].end()
        tail = text[last_date_end:last_date_end + 200]
        after_date = re.search(r"(\d{5,10})", tail)
        if after_date:
            candidate = after_date.group(1).strip()
            if not _is_date_component(candidate) and not re.fullmatch(r"\d{2}[./-]\d{2}[./-]\d{4}", candidate):
                return candidate

    # Between last two dates
    if len(date_matches) >= 2:
        second_last_end = date_matches[-2].end()
        between = text[second_last_end:date_matches[-1].start()]
        between_match = re.search(r"(\d{7,10})", between)
        if between_match:
            return between_match.group(1).strip()

    # Context-based: "платёжное поручение" near a number
    for pattern_str in [
        r"(?:плат[её]жн[ое]*\s*поручен)[^\n]{0,50}?[№N]\s*(\d[\w\-/]*)",
        r"(?:поручен)[^\n]{0,120}?(\d{5,10})",
    ]:
        for match in re.finditer(pattern_str, text, re.IGNORECASE):
            candidate = match.group(1).strip()
            if _is_date_component(candidate):
                continue
            if re.fullmatch(r"\d{2}[./-]\d{2}[./-]\d{4}", candidate):
                continue
            if len(candidate) >= 4:
                return candidate

    # Generic: any 5-10 digit standalone number near end of text
    standalone = re.findall(r"(?<!\d)(\d{5,10})(?!\d)", text)
    for candidate in reversed(standalone):
        if not _is_date_component(candidate):
            return candidate

    return None


def _find_payment_number(text: str) -> str | None:
    match = PAYMENT_NUM_PATTERN.search(text)
    if match:
        candidate = match.group(1).strip()
        if not _is_date_component(candidate):
            return candidate

    date_pattern = re.compile(r"\d{2}[./-]\d{2}[./-]\d{4}")
    date_matches = list(date_pattern.finditer(text))

    # Look for a standalone 5-7 digit number immediately before a date
    for date_match in date_matches:
        ctx_start = max(0, date_match.start() - 20)
        ctx_before = text[ctx_start:date_match.start()]
        num_match = re.search(r"(?<!\d)(\d{5,7})(?!\d)[\s\n]*$", ctx_before)
        if num_match:
            candidate = num_match.group(1).strip()
            if not _is_date_component(candidate) and not date_pattern.fullmatch(candidate):
                return candidate

    # Pattern: № NNNNNN date
    for pat in [
        r"[№N]\s*(\d{5,7})[\s\n]*\d{2}[./-]\d{2}[./-]\d{4}",
        r"(?:номер|платеж|плат)[^\n]{0,30}?[№N]?\s*(\d{5,7})",
    ]:
        for match in re.finditer(pat, text, re.IGNORECASE):
            candidate = match.group(1).strip()
            if _is_date_component(candidate):
                continue
            if not date_pattern.fullmatch(candidate) and len(candidate) >= 5:
                return candidate

    # General pattern: any standalone 5-7 digit number before a date
    for match in re.finditer(r"(\d{5,7})[\s\n]*\d{2}[./-]\d{2}[./-]\d{4}", text):
        candidate = match.group(1).strip()
        # Need to make sure it's not part of a longer number
        full_match_end = match.end(1)
        if full_match_end < len(text) and text[full_match_end].isdigit():
            continue
        if not _is_date_component(candidate) and not date_pattern.fullmatch(candidate):
            return candidate

    return None


def _extract_basis(text: str) -> str | None:
    for pattern in BASIS_PATTERNS:
        match = pattern.search(text)
        if match:
            raw = match.group(1) if match.lastindex else match.group(0)
            cleaned = re.sub(r"\s+", " ", raw).strip(" ,;:\t\n\r")
            if len(cleaned) > 3:
                return cleaned[:500]
    return None


def _extract_purpose(text: str) -> str | None:
    for pattern in PURPOSE_PATTERNS:
        match = pattern.search(text)
        if match:
            raw = match.group(1) if match.lastindex else match.group(0)
            cleaned = re.sub(r"\s+", " ", raw).strip(" ,;:\t\n\r")
            if len(cleaned) > 3:
                return cleaned[:500]
    return None


def _extract_bank_details(text: str) -> dict[str, Any]:
    """Извлекает банковские реквизиты из текста документа."""
    details = {
        "inn_sender_full": None,
        "kpp_sender": None,
        "account_sender": None,
        "bank_sender": None,
        "bik_sender": None,
        "corr_account_sender": None,
        "inn_receiver_full": None,
        "kpp_receiver": None,
        "account_receiver": None,
        "bank_receiver": None,
        "bik_receiver": None,
        "corr_account_receiver": None,
    }

    # Поиск ИНН (10 или 12 цифр)
    inn_matches = list(INN_PATTERN.finditer(text))
    for idx, match in enumerate(inn_matches):
        inn = match.group(1)
        context_start = max(0, match.start() - 200)
        context_end = min(len(text), match.end() + 100)
        context = text[context_start:context_end]

        if idx == 0:
            details["inn_sender_full"] = inn
            # Ищем КПП рядом
            kpp_match = KPP_PATTERN.search(context)
            if kpp_match:
                details["kpp_sender"] = kpp_match.group(1)
        elif idx == 1:
            details["inn_receiver_full"] = inn
            kpp_match = KPP_PATTERN.search(context)
            if kpp_match:
                details["kpp_receiver"] = kpp_match.group(1)

    # Поиск БИК (9 цифр, начинается с 0 или 1)
    bik_matches = BANK_PATTERNS["bik"].findall(text)
    if len(bik_matches) >= 1:
        details["bik_sender"] = bik_matches[0]
    if len(bik_matches) >= 2:
        details["bik_receiver"] = bik_matches[1]

    # Поиск расчетных счетов (20 цифр)
    account_matches = BANK_PATTERNS["account"].findall(text)
    for idx, acc in enumerate(account_matches):
        if idx == 0:
            details["account_sender"] = acc
        elif idx == 1:
            details["account_receiver"] = acc

    # Поиск корреспондентских счетов
    corr_matches = BANK_PATTERNS["corr_account"].findall(text)
    if len(corr_matches) >= 1:
        details["corr_account_sender"] = corr_matches[0]
    if len(corr_matches) >= 2:
        details["corr_account_receiver"] = corr_matches[1]

    # Поиск названий банков - ищем после БИК в тексте
    lines = text.split('\n')
    bank_lines = []
    for i, line in enumerate(lines):
        # Пропускаем служебные строки таблицы
        if any(kw in line.lower() for kw in ["поступ", "банк плат", "срок плат", "статус", "назнач", "вид платежа"]):
            continue
        line_clean = re.sub(r'[\n\r]+', ' ', line).strip()
        # Ищем строки с названиями банков (содержат "банк", "отделение", но не служебные)
        if re.search(r'(?:банк|отделение|уфк|ркц)\s+[А-Яа-яё]', line, re.IGNORECASE):
            if len(line_clean) > 10 and len(line_clean) < 200:
                bank_lines.append((i, line_clean))

    # Первый банк - плательщика, второй - получателя
    if len(bank_lines) >= 1:
        details["bank_sender"] = bank_lines[0][1][:150]
    if len(bank_lines) >= 2:
        details["bank_receiver"] = bank_lines[1][1][:150]

    return details


def _extract_payment_purpose_details(text: str) -> dict[str, Any]:
    """Извлекает детали из назначения платежа: номер и дата счета-фактуры, номер и дата договора."""
    details = {
        "invoice_number": None,
        "invoice_date": None,
        "contract_number": None,
        "contract_date": None,
        "amount_words": None,
        "operation_type": None,
        "payment_queue": None,
        "uin": None,
    }

    # Номер счета-фактуры (с привязкой к контексту)
    for match in INVOICE_NUM_PATTERN.finditer(text):
        # Проверяем что это счет-фактура, а не просто число
        start = max(0, match.start() - 30)
        context = text[start:match.end() + 10].lower()
        if any(kw in context for kw in ["сч/ф", "счет", "фактур"]):
            details["invoice_number"] = match.group(1)
            break

    # Дата счета-фактуры
    for match in INVOICE_DATE_PATTERN.finditer(text):
        start = max(0, match.start() - 30)
        context = text[start:match.end() + 10].lower()
        if any(kw in context for kw in ["сч/ф", "счет", "фактур"]):
            details["invoice_date"] = match.group(1)
            break

    # Номер договора
    for match in CONTRACT_NUM_PATTERN.finditer(text):
        start = max(0, match.start() - 20)
        context = text[start:match.end() + 10].lower()
        if "дог" in context or "контракт" in context or "договор" in context:
            details["contract_number"] = match.group(1)
            break

    # Дата договора
    for match in CONTRACT_DATE_PATTERN.finditer(text):
        start = max(0, match.start() - 30)
        context = text[start:match.end() + 10].lower()
        if "дог" in context or "контракт" in context or "договор" in context:
            details["contract_date"] = match.group(1)
            break

    # Сумма прописью (ищем в начале документа)
    amount_words_match = AMOUNT_WORDS_PATTERN.search(text[:500])
    if amount_words_match:
        details["amount_words"] = amount_words_match.group(0)[:200]

    # Вид операции
    match = OPERATION_TYPE_PATTERN.search(text)
    if match:
        details["operation_type"] = match.group(1)

    # Очередность платежа
    match = QUEUE_PATTERN.search(text)
    if match:
        queue_val = match.group(1)
        if queue_val.isdigit() and 1 <= int(queue_val) <= 5:
            details["payment_queue"] = queue_val

    # УИН
    match = UIN_PATTERN.search(text)
    if match:
        uin_val = match.group(1)
        if uin_val == "0" or (uin_val.isdigit() and len(uin_val) in [20, 25]):
            details["uin"] = uin_val

    return details


def _extract_organization_names(text: str) -> dict[str, str | None]:
    """Извлекает названия организаций плательщика и получателя из текста документа."""
    result = {"supplier": None, "buyer": None}

    inn_matches = list(INN_PATTERN.finditer(text))

    org_prefixes = r"(?:ООО|ЗАО|ОАО|АО|АО\s|ООО\s|\"|\"|«|Общество\sс\sограниченной|Индивидуальный\sпредприниматель|Филиал|департамент|управление|служба)"

    for idx, match in enumerate(inn_matches):
        inn = match.group(1)
        inn_start = match.start()
        inn_end = match.end()

        # Берем контекст ДО и ПОСЛЕ ИНН
        context_before = text[max(0, inn_start - 300):inn_start]
        context_after = text[inn_end:min(len(text), inn_end + 150)]

        # Ищем название организации перед ИНН
        org_pattern = re.search(
            r'(?:^|\n)([А-Я][^\n]{5,150}?(?:' + org_prefixes + r'|\d{10,12}))',
            context_before,
            re.IGNORECASE
        )

        if org_pattern:
            org_name = org_pattern.group(1).strip()
            # Убираем мусор в конце
            org_name = re.sub(r'[\s\d]{5,}$', '', org_name).strip()
            if org_name:
                if idx == 0:
                    result["supplier"] = org_name
                else:
                    result["buyer"] = org_name

    return result


def _extract_counterparties(text: str) -> tuple[dict, dict]:
    supplier = {"name": None, "inn": None, "kpp": None}
    buyer = {"name": None, "inn": None, "kpp": None}

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    joined = "\n".join(lines)

    supplier_match = ROLE_PATTERNS["supplier"].search(joined)
    buyer_match = ROLE_PATTERNS["buyer"].search(joined)

    if supplier_match:
        raw_name = supplier_match.group(1).strip()
        supplier = _parse_counterparty_info(raw_name)
        if not supplier["name"]:
            supplier["name"] = raw_name

    if buyer_match:
        raw_name = buyer_match.group(1).strip()
        buyer = _parse_counterparty_info(raw_name)
        if not buyer["name"]:
            buyer["name"] = raw_name

    inn_entries = _extract_inn_from_text(text)
    
    if inn_entries:
        inns_by_pos = sorted(inn_entries, key=lambda x: x[1])
        if not supplier["inn"] and len(inns_by_pos) >= 1:
            supplier["inn"] = inns_by_pos[0][0]
        if not buyer["inn"] and len(inns_by_pos) >= 2:
            buyer["inn"] = inns_by_pos[1][0]
        elif not buyer["inn"] and len(inns_by_pos) == 1 and supplier["inn"] and inns_by_pos[0][0] != supplier["inn"]:
            buyer["inn"] = inns_by_pos[0][0]
        elif not buyer["inn"] and len(inns_by_pos) == 1 and not supplier["inn"]:
            supplier["inn"] = inns_by_pos[0][0]

    return supplier, buyer


def _parse_counterparty_info(raw: str) -> dict:
    cp = {"name": None, "inn": None, "kpp": None}
    inn_match = INN_PATTERN.search(raw)
    if inn_match:
        cp["inn"] = inn_match.group(1)
    kpp_match = KPP_PATTERN.search(raw)
    if kpp_match:
        cp["kpp"] = kpp_match.group(1)
    name_part = re.split(r"ИНН|КПП|ОГРН|ОГРНИП|БИК|р/с|к/с|расч[её]тный|корр?[её]?", raw, flags=re.IGNORECASE)[0]
    name = re.sub(r"\s+", " ", name_part).strip(" ,;:	\n\r")
    if name and len(name) > 2:
        cp["name"] = name[:300]
    return cp


def _extract_kpp_from_text(text: str) -> list[tuple[str, int]]:
    results: list[tuple[str, int]] = []
    for match in KPP_PATTERN.finditer(text):
        results.append((match.group(1), match.start()))
    return results


def _extract_inn_from_text(text: str) -> list[tuple[str, int]]:
    results: list[tuple[str, int]] = []
    for match in INN_PATTERN.finditer(text):
        results.append((match.group(1), match.start()))
    return results


def _find_org_names_near_inn(text: str) -> dict[str, str | None]:
    result: dict[str, str | None] = {"supplier": None, "buyer": None}
    inn_positions = [(match.group(1), match.start()) for match in INN_PATTERN.finditer(text)]

    org_prefixes = r"(?:ООО|ЗАО|ОАО|АО|ПАО|ГУП|МУП|ФГУП|ИП|ГБУ|МБУ|МОУ|МКУ|ГКУ|МКДОУ|ГУ|МУ|Департамент|Управление|Администраци|Комитет|Министерство|Федеральн|Государственн)"

    for idx, (inn, pos) in enumerate(inn_positions):
        context_start = max(0, pos - 400)
        context_end = min(len(text), pos + 200)
        context = text[context_start:context_end]

        # Try to find org name with known prefixes
        org_match = re.search(
            org_prefixes + r'[\s«"][^\n]{3,150}',
            context,
            re.IGNORECASE,
        )
        if org_match:
            name = re.sub(r"\s+", " ", org_match.group(0)).strip()
            # Clean up: remove trailing numbers and special chars
            name = re.sub(r'[\d]{5,}\s*$', '', name).strip()
            if idx == 0:
                result["supplier"] = name
            else:
                result["buyer"] = name
            continue

        # Try quoted name
        org_match2 = re.search(
            r'["«][^"»\n]{4,150}["»]',
            context,
        )
        if org_match2:
            quoted = org_match2.group(0)
            # Look for prefix before the quote
            prefix_start = max(0, org_match2.start() - 80)
            before_quote = context[prefix_start:org_match2.start()]
            prefix_match = re.search(org_prefixes + r'\s*$', before_quote, re.IGNORECASE)
            if prefix_match:
                name = re.sub(r"\s+", " ", prefix_match.group(0) + " " + quoted).strip()
            else:
                name = quoted.strip()
            if idx == 0:
                result["supplier"] = name
            else:
                result["buyer"] = name

    return result


def _extract_items_from_text(text: str) -> list[dict[str, Any]]:
    """Извлекает товары/услуги из текста документа."""
    items: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Пытаемся найти дату в названии товара
        date_in_name = re.search(r"\d{2}[./-]\d{2}[./-]\d{4}", line)

        match = ITEM_LINE_PATTERN.match(line)
        if match:
            items.append({
                "name": match.group("name").strip(),
                "quantity": _normalize_amount(match.group("qty")),
                "price": _normalize_amount(match.group("price")),
                "amount": _normalize_amount(match.group("sum")),
                "date": date_in_name.group(0) if date_in_name else "",
            })
            continue

        # Попытка 2: название + сумма (однострочный формат)
        simple_match = re.match(r"^(.+?)\s+([\d\s.,]+)\s*$", line)
        if simple_match and len(simple_match.group(1)) > 3:
            amount = _normalize_amount(simple_match.group(2))
            if amount and float(amount.replace(",", ".")) > 0:
                items.append({
                    "name": simple_match.group(1).strip(),
                    "quantity": "1",
                    "price": amount,
                    "amount": amount,
                    "date": date_in_name.group(0) if date_in_name else "",
                })
    return items


def _extract_items_from_tables(tables: list[Any]) -> list[dict[str, Any]]:
    """Извлекает товары/услуги из таблиц документа."""
    items: list[dict[str, Any]] = []
    for table in tables:
        if isinstance(table, dict):
            rows = table.get("rows") or table.get("cells") or table.get("data") or []
        else:
            rows = table
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                values = [str(value).strip() for value in row.values() if str(value).strip()]
            elif isinstance(row, list):
                values = [str(value).strip() for value in row if str(value).strip()]
            else:
                values = [str(row).strip()]

            if len(values) >= 2:
                # Ищем дату в первом столбце (название)
                date_in_first = re.search(r"\d{2}[./-]\d{2}[./-]\d{4}", values[0])

                if len(values) >= 4:
                    # Полная строка: название, количество, цена, сумма
                    items.append({
                        "name": values[0],
                        "quantity": _normalize_amount(values[1]) if values[1].replace(",", ".").replace(" ", "").replace("\xa0", "").replace("\n", "").replace("\t", "").rstrip(".,-–") else "1",
                        "price": _normalize_amount(values[2]) or "",
                        "amount": _normalize_amount(values[3]) or "",
                        "date": date_in_first.group(0) if date_in_first else "",
                    })
                elif len(values) == 3:
                    # Название, количество/цена, сумма
                    items.append({
                        "name": values[0],
                        "quantity": "1",
                        "price": _normalize_amount(values[1]) or "",
                        "amount": _normalize_amount(values[2]) or "",
                        "date": date_in_first.group(0) if date_in_first else "",
                    })
                elif len(values) == 2:
                    # Название + сумма
                    amount = _normalize_amount(values[1])
                    if amount:
                        items.append({
                            "name": values[0],
                            "quantity": "1",
                            "price": amount,
                            "amount": amount,
                            "date": date_in_first.group(0) if date_in_first else "",
                        })
    return items


def _extract_vat_and_discounts(text: str, total_amount_raw: str | None) -> tuple[str|None, str|None, str|None, str|None]:
    """Извлекает НДС, сумму с НДС, сумму без НДС и скидки из текста документа."""
    amount_with_vat = _normalize_amount(total_amount_raw) if total_amount_raw else None
    amount_without_vat = None
    vat_amount = None
    discounts = None

    # Проверка на "Без НДС"
    if re.search(r"без\s*ндс|ндс\s*не\s*облагается|без\s* налога\s*на\s*добавленную", text, re.IGNORECASE):
        vat_amount = "0.00"
        amount_without_vat = amount_with_vat
        return amount_without_vat, amount_with_vat, vat_amount, discounts

    # Поиск НДС с использованием паттернов
    for pattern in VAT_PATTERNS:
        match = pattern.search(text)
        if match:
            vat_val = _normalize_amount(match.group(1))
            if vat_val:
                vat_amount = vat_val
                break

    # Альтернативный поиск: "В т.ч. НДС" в таблицах/числах
    if not vat_amount:
        # Ищем "В т.ч." и рядом число
        vat_with_words = re.search(r"(?:в\s*т\.ч\.?|в\s*том\s*числе)[^\d]*([\d\s.,]+)", text, re.IGNORECASE)
        if vat_with_words:
            vat_val = _normalize_amount(vat_with_words.group(1))
            if vat_val and float(vat_val) > 0:
                vat_amount = vat_val

    # Вычисляем сумму без НДС
    if vat_amount and amount_with_vat:
        try:
            amount_without_vat = str(round(float(amount_with_vat) - float(vat_amount), 2))
        except ValueError:
            pass

    # Если есть сумма без НДС, а НДС не найден - попробуем вычислить
    if not vat_amount and amount_with_vat and amount_without_vat:
        try:
            vat_amount = str(round(float(amount_with_vat) - float(amount_without_vat), 2))
        except ValueError:
            pass

    # Поиск скидок
    for pattern in DISCOUNT_PATTERNS:
        match = pattern.search(text)
        if match:
            dis_val = _normalize_amount(match.group(1))
            if dis_val:
                discounts = dis_val
                break

    return amount_without_vat, amount_with_vat, vat_amount, discounts

def _extract_requisites(text: str, tables: list[Any]) -> ExtractedDocument:
    document_type = _find_document_type(text)
    supplier, buyer = _extract_counterparties(text)

    warnings: list[str] = []
    
    payment_order_number = _find_payment_order_number(text)
    payment_number = _find_payment_number(text)
    if not payment_number and payment_order_number:
        payment_number = payment_order_number

    date = _find_document_date(text)
    total_amount = _find_total_amount(text)
    
    if not total_amount:
        amount_match = re.search(r"(\d{1,10}-\d{2})", text)
        if amount_match:
            total_amount = amount_match.group(1)
            total_amount = total_amount.replace("-", ".")
            
    basis = _extract_basis(text)
    purpose = _extract_purpose(text)

    amount_without_vat, amount_with_vat, vat_amount, discounts = _extract_vat_and_discounts(text, total_amount)

    # Извлекаем банковские реквизиты
    bank_details = _extract_bank_details(text)

    # Извлекаем детали из назначения платежа
    purpose_details = _extract_payment_purpose_details(purpose or text)

    raw_items = _extract_items_from_tables(tables)
    if not raw_items:
        raw_items = _extract_items_from_text(text)
        
    items = []
    for ri in raw_items:
        name = ri.get("name", "")
        item = ItemData(
            name=name,
            time_or_date=ri.get("date", ""),
            quantity=str(ri.get("quantity", "")),
            price=str(ri.get("price", "")),
            amount=str(ri.get("amount", "")),
            short_description=name
        )
        # Пытаемся извлечь дату из названия товара
        if not item.time_or_date:
            date_match = re.search(r"\d{2}[./-]\d{2}[./-]\d{4}", name)
            if date_match:
                item.time_or_date = date_match.group(0)
        items.append(item)

    short_desc = f"{document_type or 'Документ'}"
    if payment_number:
        short_desc += f" №{payment_number}"
    if date:
        short_desc += f" от {date}"

    is_payment_order = (document_type == "Платёжное поручение")

    # Извлекаем названия организаций из текста
    org_names = _extract_organization_names(text)

    исполнитель_name = org_names.get("supplier", "") or supplier.get("name") or ""
    исполнитель_inn = supplier.get("inn") or ""
    заказчик_name = org_names.get("buyer", "") or buyer.get("name") or ""
    заказчик_inn = buyer.get("inn") or ""

    исполнитель_full = исполнитель_name if исполнитель_name else (f"ИНН {исполнитель_inn}" if исполнитель_inn else None)
    заказчик_full = заказчик_name if заказчик_name else (f"ИНН {заказчик_inn}" if заказчик_inn else None)

    important = ImportantData(
        номер=payment_number,
        дата=date,
        inn_receiver=buyer.get("inn") if is_payment_order else supplier.get("inn"),
        inn_sender=supplier.get("inn") if is_payment_order else buyer.get("inn"),
        delivery=purpose or basis,
        amount_without_vat=amount_without_vat,
        amount_with_vat=amount_with_vat,
        vat_amount=vat_amount,
        discounts=discounts,
        исполнитель=исполнитель_full or None,
        заказчик=заказчик_full or None,
        # Банковские реквизиты плательщика
        inn_sender_full=bank_details.get("inn_sender_full"),
        kpp_sender=bank_details.get("kpp_sender"),
        account_sender=bank_details.get("account_sender"),
        bank_sender=bank_details.get("bank_sender"),
        bik_sender=bank_details.get("bik_sender"),
        corr_account_sender=bank_details.get("corr_account_sender"),
        # Банковские реквизиты получателя
        inn_receiver_full=bank_details.get("inn_receiver_full"),
        kpp_receiver=bank_details.get("kpp_receiver"),
        account_receiver=bank_details.get("account_receiver"),
        bank_receiver=bank_details.get("bank_receiver"),
        bik_receiver=bank_details.get("bik_receiver"),
        corr_account_receiver=bank_details.get("corr_account_receiver"),
        # Дополнительные поля
        amount_words=purpose_details.get("amount_words"),
        operation_type=purpose_details.get("operation_type"),
        payment_queue=purpose_details.get("payment_queue"),
        uin=purpose_details.get("uin"),
        # Реквизиты из назначения платежа
        invoice_number=purpose_details.get("invoice_number"),
        invoice_date=purpose_details.get("invoice_date"),
        contract_number=purpose_details.get("contract_number"),
        contract_date=purpose_details.get("contract_date"),
    )
    details = DetailsData(
        items=items,
        short_document_description=short_desc
    )

    return ExtractedDocument(
        important=important,
        details=details,
        document_type=document_type,
        source_file=None,
        text=text,
        warnings=warnings
    )


def _coerce_response_text(response_data: dict[str, Any], local_text: str) -> str:
    text = _join_text_from_response(response_data)
    if text.strip():
        return text
    return local_text


def _build_result(
    extracted: ExtractedDocument,
    output_format: str,
    source_file: str,
    language: str,
) -> dict[str, Any] | str:
    result = extracted.to_dict()
    result.update({
        "source_file": source_file,
        "language": language,
        "ok": len(extracted.warnings) <= 2,
    })
    if output_format == "json":
        return json.dumps(result, ensure_ascii=False, indent=2)
    return result


def _build_table_result(extracted: ExtractedDocument, source_file: str) -> dict[str, Any]:
    return {
        "meta": {
            "source_file": source_file,
            "document_type": extracted.document_type,
            "ok": True,
        },
        "fields": extracted.to_dict(),
        "items": [
            {
                "name": item.name,
                "time_or_date": item.time_or_date,
                "quantity": item.quantity,
                "price": item.price,
                "amount": item.amount,
                "short_description": item.short_description,
            } for item in extracted.details.items
        ],
        "warnings": extracted.warnings,
        "raw_text_snippet": extracted.text[:2000] if extracted.text else "",
    }


@window_logger
@log_decorator
def run_pdf_yandex_vision(
    token: str,
    folder_id: str,
    file_path: str,
    language: str = "mixed",
    output_format: str = "dict",
    log_marker: bool = True,
    **kwargs
) -> dict[str, Any] | str:
    del log_marker
    
    normalized_output_format = _normalize_output_format(output_format)
    
    def _create_error_response(err_msg: str) -> dict[str, Any] | str:
        _show_error_message("Ошибка Yandex Vision", err_msg)
        result = {
            "важное": {
                "номер": None, "дата": None,
                "инн_получателя": None, "инн_отправителя": None, "поставка": None,
                "сумма_без_ндс": None, "сумма_с_ндс": None, "сумма_ндс": None, "скидки": None,
                "исполнитель": None, "заказчик": None,
                "инн_отправителя_полный": None, "кпп_отправителя": None, "счет_отправителя": None,
                "банк_отправителя": None, "бик_отправителя": None, "корр_счет_отправителя": None,
                "инн_получателя_полный": None, "кпп_получателя": None, "счет_получателя": None,
                "банк_получателя": None, "бик_получателя": None, "корр_счет_получателя": None,
                "сумма_прописью": None, "вид_операции": None, "очередность_платежа": None, "уин": None,
                "номер_счета_фактуры": None, "дата_счета_фактуры": None,
                "номер_договора": None, "дата_договора": None,
            },
            "данные": {"товары": [], "краткое_описание_документа": None},
            "метаданные": {
                "тип_документа": None,
                "исходный_файл": file_path,
                "предупреждения": [err_msg],
            },
            "ok": False,
            "error": err_msg
        }
        if normalized_output_format == "json":
            return json.dumps(result, ensure_ascii=False, indent=2)
        return result

    try:
        _ensure_dependencies()
        
        if not token or not token.strip():
            raise DocumentRecognitionError("TOKEN не может быть пустым.")
        if not folder_id or not folder_id.strip():
            raise DocumentRecognitionError("FOLDER_ID не может быть пустым.")
        if not file_path or not file_path.strip():
            raise DocumentRecognitionError("FILE_PATH не может быть пустым.")

        normalized_language = _normalize_language(language)
        pdf_base64, pdf_bytes = _read_pdf_base64(file_path)

        response_data = _post_batch_analyze(token.strip(), folder_id.strip(), pdf_base64, normalized_language)
        response_text = _coerce_response_text(response_data, _extract_pdf_text_locally(pdf_bytes))
        tables = _collect_tables(response_data)
        extracted = _extract_requisites(response_text, tables)

        result = _build_result(extracted, normalized_output_format, file_path, normalized_language)

        if isinstance(result, str):
            return result
        result["raw_response"] = response_data
        result["table_view"] = _build_table_result(extracted, file_path)
        return result
        
    except FileNotFoundError as e:
        return _create_error_response(f"Файл не найден:\n{file_path}")
    except YandexVisionAuthError as e:
        return _create_error_response(f"Ошибка авторизации:\n{str(e)}\nПроверьте токен и Folder ID.")
    except YandexVisionQuotaError as e:
        return _create_error_response(f"Превышена квота Yandex Vision:\n{str(e)}")
    except YandexVisionNetworkError as e:
        return _create_error_response(f"Ошибка сети/сервиса:\n{str(e)}")
    except YandexVisionResponseError as e:
        return _create_error_response(f"Некорректный ответ от API:\n{str(e)}")
    except Exception as e:
        return _create_error_response(f"Внутренняя ошибка обработки:\n{str(e)}")


def main(*args: Any, **kwargs: Any) -> dict[str, Any] | str:
    return run_pdf_yandex_vision(*args, **kwargs)
