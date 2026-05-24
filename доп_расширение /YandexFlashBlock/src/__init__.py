import json
import requests

try:
    from puzzle_logger import log_decorator, window_logger  # type: ignore
except Exception:
    def _identity_decorator(func=None, *args, **kwargs):
        if func is None:
            def wrapper(inner_func):
                return inner_func
            return wrapper
        return func
    log_decorator = _identity_decorator
    window_logger = _identity_decorator

IAM_TOKEN_ENDPOINT = "https://iam.api.cloud.yandex.net/iam/v1/tokens"
FOUNDATION_MODELS_ENDPOINT = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
FLASH_MODEL_TEMPLATE = "gpt://{folder_id}/yandexgpt-lite/latest"

def _exchange_oauth_to_iam(oauth_token: str) -> str | None:
    try:
        response = requests.post(
            IAM_TOKEN_ENDPOINT, 
            json={"yandexPassportOauthToken": oauth_token}, 
            timeout=60
        )
        if response.status_code == 200:
            return response.json().get("iamToken")
    except Exception:
        pass
    return None

def _post_flash_completion(auth_scheme: str, auth_token: str, folder_id: str, question: str, context_text: str) -> str:
    model_uri = FLASH_MODEL_TEMPLATE.format(folder_id=folder_id)
    payload = {
        "modelUri": model_uri,
        "completionOptions": {
            "stream": False,
            "temperature": 0.2,
            "maxTokens": 1500,
        },
        "messages": [
            {
                "role": "system",
                "text": (
                    "Ты помощник по анализу документов. Отвечай кратко, понятно и по делу. "
                    "Опирайся исключительно на переданный контекст."
                ),
            },
            {
                "role": "user",
                "text": f"Вопрос: {question}\n\nКонтекст документа:\n{context_text}",
            },
        ],
    }

    headers = {
        "Authorization": f"{auth_scheme} {auth_token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(FOUNDATION_MODELS_ENDPOINT, headers=headers, json=payload, timeout=120)
    except UnicodeEncodeError:
        raise RuntimeError("Токен содержит недопустимые символы (например, русские буквы). Проверьте правильность токена.")
    
    if response.status_code in {401, 403}:
        raise RuntimeError("Ошибка авторизации: неверный токен или недостаточно прав.")
    if response.status_code == 429:
        raise RuntimeError("Превышена квота или лимит запросов Yandex Flash.")
    if response.status_code >= 500:
        raise RuntimeError(f"Ошибка на стороне сервера Yandex: {response.status_code}")
    
    response.raise_for_status()
    data = response.json()
    
    try:
        text = data["result"]["alternatives"][0]["message"]["text"]
        return text
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Не удалось разобрать ответ от Yandex Flash: {data}")

def _show_error_message(title: str, message: str) -> None:
    try:
        import sys
        if sys.platform == "darwin":
            import subprocess
            safe_msg = message.replace('"', '\\"')
            safe_title = title.replace('"', '\\"')
            script = f'display dialog "{safe_msg}" with title "{safe_title}" buttons {{"OK"}} default button "OK" with icon stop'
            subprocess.run(["osascript", "-e", script], check=False)
        elif sys.platform == "win32":
            import ctypes
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

@window_logger
@log_decorator
def run_yandex_flash(token: str, folder_id: str, context: any, question: str, log_marker: bool = True) -> str:
    del log_marker
    
    try:
        if not token or not str(token).strip():
            raise ValueError("Токен не может быть пустым.")
        if not folder_id or not str(folder_id).strip():
            raise ValueError("Каталог (Folder ID) не может быть пустым.")
        if not question or not str(question).strip():
            raise ValueError("Вопрос не может быть пустым.")

        token = str(token).strip()
        folder_id = str(folder_id).strip()
        question = str(question).strip()
        
        if isinstance(context, dict) or isinstance(context, list):
            context_text = json.dumps(context, ensure_ascii=False, indent=2)
        else:
            context_text = str(context)
            
        if len(context_text) > 12000:
            context_text = context_text[:12000] + "\n\n[Текст обрезан]"

        for auth_scheme in ["Api-Key", "Bearer"]:
            try:
                return _post_flash_completion(auth_scheme, token, folder_id, question, context_text)
            except RuntimeError as e:
                if auth_scheme == "Bearer" and "авторизации" in str(e).lower():
                    break
                if "авторизации" not in str(e).lower():
                    raise

        iam_token = _exchange_oauth_to_iam(token)
        if iam_token:
            return _post_flash_completion("Bearer", iam_token, folder_id, question, context_text)
        
        raise RuntimeError("Не удалось авторизоваться в Yandex Cloud. Проверьте токен.")
        
    except Exception as e:
        error_msg = f"Ошибка Yandex Flash:\n{str(e)}"
        _show_error_message("Ошибка нейросети Flash", error_msg)
        return f"Ошибка: {str(e)}"

def main(*args, **kwargs):
    return run_yandex_flash(*args, **kwargs)
