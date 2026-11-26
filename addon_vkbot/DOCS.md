# Home Assistant Add-on: VK Bot

Бот ВКонтакте на Python с очередью отправки сообщений (текст, фото, видео) и HTTP API для интеграции с Home Assistant.

## Возможности

- Персистентная очередь сообщений: переживает перезапуск аддона (хранится в `/data/vkbot/queue.json`)
- Отправка текста, фото и видео в личные сообщения и в беседы VK
- Автоматическая загрузка медиа через VK upload servers (`photos.getMessagesUploadServer`, `video.save`)
- Повторы при ошибках сети/VK с настраиваемым лимитом и задержкой
- HTTP API для автоматизаций HA
- Сенсор очереди: `/health` и `/queue` — можно опрашивать `rest`-сенсором

## Настройка

1. Получите токен сообщества: VK → Управление сообществом → Работа с API → Ключи доступа → создать ключ с правом `messages` (и `photos`, `videos` для медиа).
2. Вставьте ключ в поле **vk_token** в настройках аддона.
3. Укажите **default_peer_id**, если хотите слать по умолчанию в одну цель:
   - личка: положительный `user_id`;
   - беседа: `2000000000 + chat_id` (например `2000000003`);
   - сообщество (от имени группы): `-group_id`.

## HTTP API

### `POST /send`

```json
{
  "text": "Текст сообщения",
  "photos": ["/media/photos/scene.jpg"],
  "videos": ["/media/videos/cam.mp4"],
  "peer_id": 2000000003
}
```

Ответ: `202 {"queued": true, "id": ..., "pending": N}`.

Пути к файлам — внутри контейнера: `/media/...` (маппится на медиатеку HA) и `/share/...` доступны благодаря `map: [share:rw, media:rw]`.

### `GET /health`

`{"status": "ok", "pending": 2}`

### `GET /queue`

Снимок очереди: сколько ждёт, элементы с текстом/счётчиком медиа и попыток.

## Пример интеграции

configuration.yaml:

```yaml
rest_command:
  vkbot_send:
    url: http://a0d7b954-vkbot:8099/send
    method: POST
    headers:
      Content-Type: application/json
    payload: >-
      {"text": "{{ text }}",
       "peer_id": {{ peer_id | default(2000000003) }},
       "photos": {{ photos | default([]) | tojson }},
       "videos": {{ videos | default([]) | tojson }}}

rest:
  - resource: http://a0d7b954-vkbot:8099/health
    name: vkbot_queue
    scan_interval: 30
    value_template: "{{ value_json.pending }}"
    json_attributes:
      - status
```

Автоматизация:

```yaml
automation:
  - alias: "Уведомление в VK о движении"
    trigger:
      - platform: state
        entity_id: binary_sensor.front_door_motion
        to: "on"
    action:
      - service: rest_command.vkbot_send
        data:
          text: "Движение у входной двери!"
          peer_id: 2000000003
          photos: ["/media/snapshots/door.jpg"]
```

Имя хоста `a0d7b954-vkbot` — стандартный внутренний DNS-алиас аддона; если порт в options изменён, поправьте URL.
