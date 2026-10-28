# Откат SSH-прокси для OpenCode

Это инструкция **на случай необходимости**, не действие, выполненное при публикации.
Есть два независимых уровня отката: только команда/ярлык или полное отключение
интеграции и автозапуска туннеля. Сначала выберите нужный вариант.

Все команды ниже — в **обычном отдельном Windows PowerShell из меню «Пуск»**, не
во встроенном терминале OpenCode. Пути соответствуют принятому постоянному клону
и установленному Python 3.14. Если размещение менялось, сначала проверьте пути.
Не удаляйте клон, Python, manifest или приватный config до завершения отката.

## Вариант A: вернуть обычную команду без обёртки

1. Закройте OpenCode, запущенный через обёртку/новый ярлык. Не завершайте все
   процессы `opencode`, `python` или `ssh` по имени.
2. Удалите через Проводник **только** созданный нами ярлык `OpenCode via SSH`
   на рабочем столе. Старые ярлыки OpenCode не удаляйте.
3. Выполните:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$HOME\Projects\opencode-ssh-proxy\manage-command.ps1" -Action Remove
if ($LASTEXITCODE -ne 0) { throw "Откат команды не завершён. Дальше не продолжайте." }
```

Удаляется только собственный каталог из пользовательского PATH, проверенный
по manifest exe обёртки и её `command.local.json`. Другие изменения PATH после
установки сохраняются. Точный исходный PATH с его registry type остаётся в
архивном `command-install-before-*.json` во внешнем приватном state-каталоге.
Backup содержит локальные пути: не публикуйте его целиком.

Если файлы обёртки изменены или нет manifest, удаление останавливается. Не обходите
защиту массовым удалением файлов или заменой всего PATH из старого backup.

**Этот вариант не останавливает туннель, не удаляет его автозапуск и не очищает
managed proxy env прежних экспериментов.** Для полного отключения нужен вариант B.

## Вариант B: полное отключение интеграции и автозапуска

### Перед началом

- Если для доступа к модели нужен прежний системный VPN, включите его самостоятельно.
  После отката запросы больше не направляются нашей командой через VPS.
- Сохраните работу и полностью закройте клиенты OpenCode. Изменение managed env
  штатно останавливает общий сервис и может оборвать сессию.
- Удалите через Проводник только ярлык `OpenCode via SSH` на рабочем столе.
  Ярлык supervisor в Startup удалит штатная команда ниже.

### Выполните весь блок в отдельном Windows PowerShell

Блок читает путь **оригинального нативного CLI до удаления runtime config**.
Он не использует команду `opencode` из старого PATH, которая ещё может указывать
на удалённую обёртку. Пароли, OAuth и значения managed env не выводятся.

**Условие удаления managed env:** в исходном состоянии именно этой настройки
`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` у сервиса отсутствовали; это было проверено
и сохранено до переключения. Поэтому возврат к исходному состоянию — `unset`
ровно этих трёх имён. Если после настройки вы сами добавили другие значения,
либо применяете инструкцию на другом компьютере, не запускайте этот блок вслепую:
сначала согласуйте восстановление из конкретного исходного backup. Не выбирайте
автоматически «самый новый» backup — он может уже содержать изменённые настройки.

```powershell
$ErrorActionPreference = "Stop"
$repo = Join-Path $HOME "Projects\opencode-ssh-proxy"
$state = Join-Path $HOME ".config\opencode-ssh-proxy"
$python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python314\python.exe"
$runtimeFile = Join-Path $state "command.local.json"

$native = $null
if (Test-Path -LiteralPath $runtimeFile -PathType Leaf) {
    $runtime = [IO.File]::ReadAllText($runtimeFile) | ConvertFrom-Json
    $native = $runtime.native_cli
} else {
    # Вариант A мог уже удалить runtime config: ищем native рядом с прежними shims.
    foreach ($command in @(Get-Command opencode -CommandType Application -All -ErrorAction SilentlyContinue)) {
        $candidate = Join-Path (Split-Path $command.Source) "opencode-cli.exe"
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $native = $candidate
            break
        }
    }
}
if (-not $native -or -not (Test-Path -LiteralPath $native -PathType Leaf)) {
    throw "Оригинальный CLI не найден. Дальше не продолжаем."
}

foreach ($name in @("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")) {
    & $native service unset env $name *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Не удалось восстановить managed env. Туннель пока не останавливаем."
    }
}

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$repo\manage-command.ps1" -Action Remove
if ($LASTEXITCODE -ne 0) { throw "Откат команды не завершён." }

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$repo\manage.ps1" -Action RemoveStartup -PythonExecutable "$python"
if ($LASTEXITCODE -ne 0) { throw "Не удалось удалить совпадающий ярлык автозапуска." }

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$repo\manage.ps1" -Action Stop -PythonExecutable "$python"
if ($LASTEXITCODE -ne 0) { throw "Штатная остановка туннеля не завершена. Не завершайте чужие процессы." }

# Только окружение этого отдельного PowerShell, не глобальные переменные Windows.
foreach ($name in @("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")) {
    [Environment]::SetEnvironmentVariable($name, $null, "Process")
}
& $native service start *> $null
if ($LASTEXITCODE -ne 0) { throw "Настройки восстановлены, но запуск общего сервиса не подтверждён." }

Write-Output "Обёртка и автозапуск удалены; туннель остановлен; общий сервис запущен без наших managed proxy env."
```

Если вариант A уже выполнен, блок умеет искать нативный CLI рядом с прежними
командами из PATH. Отсутствие CLI — явный отказ, не запуск удалённой обёртки.
При частичном отказе сохраните вывод и остановитесь:
не повторяйте неизвестные мутации и не редактируйте `service.json` вручную.

### Проверка результата

Закройте и заново откройте приложение терминала из меню «Пуск». Для Windows Terminal
может потребоваться закрыть все окна, а для IDE — перезапустить IDE. Затем:

```powershell
(Get-Command opencode -CommandType Application | Select-Object -First 1).Source
opencode --version
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$HOME\Projects\opencode-ssh-proxy\manage.ps1" -Action Status -PythonExecutable "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe"
```

Команда должна разрешаться в прежнюю установку OpenCode, **не** в
`.local\opencode-proxy\bin\opencode.exe`. Для полного отката ожидается `STOPPED`.
Ярлык `OpenCodeSSHProxy` должен отсутствовать в пользовательской Startup.
`--version` и работа локального сервиса не доказывают доступность модели через
прежний VPN: проверьте один настоящий ответ отдельно.

## Что намеренно остаётся

- SSH-ключ, `known_hosts`, пароль, приватный config, журналы и backups — для
  восстановления/проверки; их удаление в эту инструкцию не входит.
- Клон, Python, OpenSSH и установленный OpenCode не удаляются.
- VPS, firewall, маршруты, системный proxy и глобальные proxy env Windows не меняются.
- Прежние WireGuard/wireproxy файлы и серверные настройки — отдельная незавершённая
  история; этот откат их не удаляет и не обещает вернуть весь компьютер к состоянию
  до первых VPN-экспериментов.

Проверено: удаление/повторная установка команды, сохранение сторонних PATH edits
и удаление ярлыков автозапуска на временных fixtures. **Полный откат работающей
настройки на настоящем компьютере не выполнялся**, чтобы не отключать рабочую связь.
