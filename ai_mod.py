import logging

from openai import AsyncOpenAI

from config import config

logger = logging.getLogger(__name__)

client = AsyncOpenAI(
    api_key=config.DEEPSEEK_API_KEY.get_secret_value(),
    base_url=config.DEEPSEEK_BASE_URL,
)


async def qualify_lead(message_text: str, system_prompt: str) -> str:
    """Отправить сообщение в ИИ: вернуть готовый ответ или маркер «[НЕ ЛИД]».

    При ошибке API исключение пробрасывается: вызывающий код пропускает сообщение, а текст ошибки
    не попадает клиенту как «заявка».
    """
    try:
        response = await client.chat.completions.create(
            model=config.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message_text},
            ],
            max_tokens=500,
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AI Error: {e}")
        raise
