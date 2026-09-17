# Источники и технические решения

Дата проверки источников: 13 сентября 2026.

## Основной источник совместимости: AlexxIT/YandexStation

https://github.com/AlexxIT/YandexStation

Исследованы текущие файлы:

- `custom_components/yandex_station/core/yandex_glagol.py`, blob SHA `90108a052887032be6f1b070308c36c438ef3458`: envelope conversationToken/id/payload/sentTime, WSS, токен устройства, `_yandexio._tcp.local.`. Логика сетевого клиента написана для отдельного приложения; ожидающий ответ Future создаётся до отправки, словари состояния — на экземпляр, а не на класс.
- `custom_components/yandex_station/core/yandex_session.py`, blob SHA `a533308d819a96970322a4fc0936adadd61ce604`: QR → x-token → music token → Glagol token. Публичные идентификаторы OAuth-клиентов перенесены из upstream. Пароли, импорт browser cookies и pickle-хранилище не включены. Использовано отдельное DPAPI-хранилище.
- `custom_components/yandex_station/core/utils.py`, blob SHA `d314fd6ceff32b3b535e41805b95a105a9222b45`: `audio_play_command`, protobuf `externalCommandBypass`, `update_form`. Для HLS — format HLS/type FmRadio, для файла — format MP3/type Track. MP3 здесь является допустимым enum; фактический кодек определяется Станцией по содержимому, согласно комментарию upstream. В этом же файле описана несовместимость старой radio_play с прошивками примерно с июля 2026.
- `custom_components/yandex_station/core/yandex_station.py`, blob SHA `3d90f717c6a21947a2019415b1e690dbdd46597d`: применение аудиокоманд к локальному воспроизведению.
- `LICENSE.md`: MIT, Copyright (c) 2020 AlexxIT. Полный текст сохранён в `licenses/AlexxIT-YandexStation.txt`.

Ссылки ведут на развивающийся upstream; указаны blob SHA прочитанных файлов, а не придуманная версия релиза.

## Остальные репозитории из задания

https://github.com/SanyaPilot/YNDXRemote

Android-пульт. Проверены README, дерево master (`1473e665489301532d06d908f3d2faa02fb54c4b`), `GlagolClient.kt` и модели `QuasarClient.kt`. Подтверждены структура локального управления, mDNS и поля состояния. Kotlin-код и GPL-приложение в этот архив не копировались и не включались.

https://github.com/dext0r/ha-yandex-station-intents

Обработка команд Алисы в Home Assistant через сценарии. Это не захват Windows-аудио и не протокол его доставки. Использован как архитектурный контекст, зависимость в приложение не добавлена.

https://github.com/dext0r/yandex_smart_home

Представление устройств Home Assistant в умном доме. Не аудиодрайвер и не LAN-аудиотранспорт для ПК. Приложение не требует его установки.

## Дополнительный источник

https://github.com/volodarskij/yandex-station-local-streaming
https://github.com/volodarskij/yandex-station-local-streaming/blob/master/docs/04-http-stream.md

Автор описывает поведение старого радио-плеера Станции: конечные ресурсы, честный Content-Length, уникальные URL, трудности бесконечного chunked MP3. Это опыт другого устройства/режима, а не доказательство работы на Миди. Из него нельзя переносить точное значение задержки на наш HLS. До 1.0.4 в Station Bridge был только HLS с конечными сегментами. В 1.0.5 добавлен прямой MP3 с конечным бюджетом кадров и вычисляемой длиной ответа; подробности ниже. Старая radio_play не используется. Реализация этого репозитория в архив не скопирована.

## Документация и зависимости

WASAPI loopback, Microsoft:
https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording

HLS muxer, FFmpeg:
https://ffmpeg.org/ffmpeg-formats.html#hls-2

PyAudioWPatch 0.2.12.8, Windows WASAPI loopback:
https://pypi.org/project/PyAudioWPatch/0.2.12.8/

imageio-ffmpeg 0.6.0, доставка FFmpeg в колёсах для поддерживаемых платформ:
https://pypi.org/project/imageio-ffmpeg/0.6.0/

Python 3.13.15 x64 embeddable, официальный релиз:
https://www.python.org/downloads/release/python-31315/
https://www.python.org/ftp/python/3.13.15/python-3.13.15-embed-amd64.zip
Опубликованный SHA-256: `d1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf`.

https://pypi.org/project/aiohttp/3.14.3/
https://pypi.org/project/zeroconf/0.151.3/
https://pypi.org/project/qrcode/8.2/

Windows runtime и зависимости скачиваются при запуске, а не включены в этот ZIP. Их лицензии поставляются в исходных дистрибутивах. При дальнейшем распространении сборки вместе с FFmpeg нужно отдельно соблюдать лицензию именно выбранной сборки FFmpeg и требования к соответствующим исходным текстам; лицензия MIT этого приложения их не заменяет.


## Исправление сетевого адреса 1.0.2

Сопоставление IP/PrefixLength и ограничения правил проверены по официальной документации Microsoft:
https://learn.microsoft.com/en-us/powershell/module/nettcpip/get-netipaddress?view=windowsserver2025-ps
https://learn.microsoft.com/en-us/powershell/module/nettcpip/get-netipconfiguration?view=windowsserver2025-ps
https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallrule?view=windowsserver2025-ps

Сам алгоритм выбора обратного HTTP-адреса написан для Station Bridge. Успех
исходящего соединения не трактуется как доказательство обратной доступности.
Классификация по именам адаптеров — эвристика; 10.x не является признаком VPN.


## Дополнение 1.0.3: NAT и точечное разрешение аудиопосредника

Обновление основано на разборе локального MediaServer и полях предоставленного
диагностического отчёта: шесть входящих запросов, шесть отказов peer_not_allowed,
различающиеся IP Glagol и HTTP. Подтверждён отказ приложения; наличие конкретного
маршрутизатора/NAT по JSON не доказывается.

Описание механизма замены исходного IP (справочный материал, не сведения о сети пользователя):
https://learn.microsoft.com/en-us/azure/vpn-gateway/nat-overview

Логика access.py написана для этой программы: явное подтверждение точного
адреса в localhost-панели, действующая случайная ссылка, привязка разрешения
к выбранной Станции/сертификату/IP компьютера. Исходные библиотеки не изменялись.


## 1.0.4 — источники для проверки аудиовхода

- Microsoft, Loopback Recording: https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording
  WASAPI loopback относится к выбранному render endpoint, не автоматически ко всем выходам.
- PyAudioWPatch: https://github.com/s0d3s/PyAudioWPatch
  https://github.com/s0d3s/PyAudioWPatch/blob/master/examples/pawp_record_wasapi_loopback.py
  API перечисления loopback и пример paInt16 callback. Новая диагностика и поиск
  сигнала написаны отдельно; готовая реализация поиска из этого примера не копировалась.
- Пользовательский отчёт версии 1.0.3: 21 принятый запрос, 0 отклонённых,
  12 запросов сегментов; в момент отчёта level=0, captured_seconds=3.5,
  stream_seconds=12.5, inserted_silence_seconds=9. Это свидетельство недостатка
  кадров/сигнала, но не доказательство конкретного выбранного устройства или
  успешного декодирования HLS колонкой. Сам отчёт пользователя в архив не включён.


## Изменения 1.0.5 — задержка

- FFmpeg, Format Options и HLS/MP3 muxers:
  https://ffmpeg.org/ffmpeg-formats.html
  probesize/analyzeduration и flush_packets проверены по документации; результат
  проверен на локальном кодировщике. Параметры PCM полностью заданы приложением.
- FFmpeg, libmp3lame encoder:
  https://ffmpeg.org/ffmpeg-codecs.html
  Прямой MP3 использует CBR и отключённый bit reservoir, чтобы повторное
  подключение не требовало кадров предыдущего HTTP-ответа.
- Music Assistant, Yandex Station provider:
  https://www.music-assistant.io/player-support/yandex-station/
  Документация отмечает необходимость Content-Length вместо chunked для Станций.
  Это не результат проверки Station Bridge 1.0.5 на физической колонке.
- RFC 8216, раздел 6.3.3:
  https://www.rfc-editor.org/rfc/rfc8216.html
  Объясняет компромисс между стартом у живого края и устойчивостью HLS.
- Собственная реализация 1.0.5: FrameRing, MP3FrameParser, ограниченный поток
  HTTP, ограничения очереди PCM и кнопка ручного возврата к живому звуку.
  Новые фрагменты стороннего исходного кода для этих изменений не копировались.


## 1.0.6 — PCM и уменьшение очередей

Повторно прочитан AlexxIT/YandexStation `utils.py` (тот же SHA
`d314fd6ceff32b3b535e41805b95a105a9222b45`): комментарий подтверждает определение
кодека из содержимого WAV при enum MP3. Он НЕ подтверждает малую задержку или
работу длительного WAV-потока. Эти аспекты новой реализации требуют проверки.

https://www.music-assistant.io/player-support/yandex-station/
Требование Content-Length учтено для длительного WAV: заголовок RIFF и длина
HTTP вычисляются из одного конечного бюджета блоков; досрочная остановка обрывает
соединение, а не выдаёт ложное успешное окончание передачи.

https://learn.microsoft.com/en-us/windows-hardware/drivers/audio/low-latency-audio
Размеры/периоды буферов драйвера и стадии задержки. Запрошенные 10 мс в
PyAudio не объявляются гарантированным периодом драйвера или слышимой задержкой.

https://learn.microsoft.com/en-us/windows/win32/api/timeapi/nf-timeapi-timebeginperiod
https://learn.microsoft.com/en-us/windows/win32/api/avrt/nf-avrt-avsetmmthreadcharacteristicsw
https://learn.microsoft.com/en-us/windows/win32/api/avrt/nf-avrt-avrevertmmthreadcharacteristics
Временный запрос таймера и класс Audio для потока писателя, парное освобождение.
Без правок реестра/процесса REALTIME; возможен рост энергопотребления. API могут
отказать, а Windows не гарантирует результат при всех состояниях приложения.

PCM transport, latency profiles, residual-buffer expiry and comparison page
are application-specific implementations, not copied drivers/firmware.
User diagnostic JSON is not included. No receiver-buffer control API is claimed.
