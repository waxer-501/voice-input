# Voice Input

Linux 下的按住说话（push-to-talk）语音输入工具，基于火山引擎流式 ASR，识别结果直接粘贴到光标位置。

## 特性

- **按住说话**：按住 `Ctrl+Win` 开始录音，松开自动粘贴识别结果到当前光标
- **实时流式识别**：录音过程中音频实时上传，松手后几乎立即出结果
- **shift+Insert 粘贴**：绕过部分应用（如 OpenCode TUI）的剪贴板图片检测，避免文字被误识别为 `[Image #N]`
- **即时停止录音**：松手瞬间杀掉 arecord 进程，防止录音堆积
- **CJK 友好**：通过剪贴板粘贴而非键盘模拟，可靠支持中日韩输入

## 文件

| 文件 | 说明 |
|------|------|
| `voice_input.py` | 守护进程，热键驱动按住说话，松手粘贴 |
| `talky.py` | 命令行工具，支持 file / mic / daemon 多种模式 |
| `replacements.py` | 替换词管理：校验源文件、生成上传文件、上传到火山引擎 |
| `replacements.txt` | 替换词源文件（可维护，每行 `源词\|目标词`，# 开头为注释） |

## 依赖

```bash
# 系统工具
sudo apt install xclip xdotool alsa-utils pulseaudio-utils

# Python 依赖
pip install websockets pynput sounddevice
```

## 配置

在同目录创建 `.env` 文件（参考 `.env.example`）：

```env
VOLC_APPID=你的_appid
VOLC_TOKEN=你的_token
VOLC_RESOURCE=volc.seedasr.sauc.duration
```

凭据从 [火山引擎控制台 - 语音技术](https://console.volcengine.com/speech/app) 获取。

## 使用

### voice_input.py — 按住说话守护进程

```bash
python3.10 voice_input.py
```

- 按住 `Ctrl+Win` 开始录音（听到提示音）
- 松开结束录音，识别结果自动粘贴到光标位置
- `Ctrl+C` 退出

### talky.py — 命令行转写工具

```bash
# 转写 WAV 文件（需 16kHz/16bit/mono）
python3.10 talky.py file audio.wav

# 录制 N 秒并转写
python3.10 talky.py mic 5

# 守护模式：录到 SIGTERM/SIGINT
python3.10 talky.py daemon

# IME 模式：后台启动 / 停录并注入
python3.10 talky.py start
python3.10 talky.py stop
```

### replacements.py — 替换词管理

维护 `replacements.txt`（每行 `源词|目标词`，`#` 开头为注释），然后一键上传到火山引擎替换词表。上传后需在控制台把词表绑定到 ASR 应用才生效。

```bash
# 仅校验源文件格式
python3.10 replacements.py --check

# 生成上传文件，不上传（输出 replacements_upload.txt）
python3.10 replacements.py --no-upload

# 校验 + 生成 + 上传（需要 IAM AK/SK）
python3.10 replacements.py
```

上传需要 `.env` 中额外配置 `VOLC_AK` / `VOLC_SK`（IAM 密钥，与 ASR 的 `VOLC_TOKEN` 不同），从 [IAM 密钥管理](https://console.volcengine.com/iam/keymanage/) 获取。脚本会自动判断词表是否存在：已存在则更新，不存在则创建。

## 工作原理

1. 按下热键时，`arecord` 开始采集 16kHz/16bit/mono PCM
2. 音频块实时通过 WebSocket 流式发送到火山引擎 ASR
3. 松开热键时，立即终止 `arecord`，发送 final 标记
4. 收到 ASR 最终结果后，写入 X11 PRIMARY + CLIPBOARD 两个 selection
5. 通过 `xdotool key shift+Insert` 触发粘贴
6. 若剪贴板操作失败，回退到 `xdotool type` 逐字注入
7. 粘贴完成后恢复原始剪贴板内容

## 许可证

MIT
