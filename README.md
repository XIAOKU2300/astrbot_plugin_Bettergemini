# astrbot_plugin_Bettergemini

> 基于 Flow2API 的 AstrBot 全能视觉插件，支持 AI 智能体原生 Tool 调用，实现聊天式文生图、图生图、文生视频、图生视频。

![预览图](<img width="810" height="6531" alt="image" src="https://github.com/user-attachments/assets/5c530ff6-edd0-442c-a3b7-92089fd3c861" />
)

---

## 功能特性

- **AI 智能体直接调用**：无需记忆指令，自然语言对话即可触发绘图或视频生成
- **文生图 / 图生图**：支持 Gemini 2.5/3.0、Imagen 4.0 多引擎切换
- **文生视频 / 图生视频**：基于 Veo 3.1，支持横竖屏自动识别
- **快捷咒语系统**：可自定义快捷词一键触发预设 Prompt
- **亚洲审美预设**：自动为 Prompt 叠加高质量风格词
- **图片自适应压缩**：超大图自动缩放，支持 HTTP 链接与本地路径

---

## 安装

1. 将插件目录放入 AstrBot 的 `plugins/` 文件夹
2. 安装依赖：
   ```bash
   pip install aiohttp Pillow
   ```
3. 在 AstrBot 管理面板重载插件

---

## 配置

在插件配置面板填写以下参数：

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `api_url` | Flow2API 的 OpenAI 兼容接口地址 | `http://127.0.0.1:8000/v1/chat/completions` |
| `apikey` | Flow2API 鉴权密钥 | *(必填)* |
| `model` | 默认绘图模型 | `imagen-4.0-generate-preview-landscape` |
| `custom_model` | 自定义私有模型 ID（可选） | — |
| `aesthetic_tags` | 自动叠加的风格 Tag | 亚洲高级审美预设 |
| `prompt_list` | 快捷咒语列表，格式：`指令词:完整Prompt` | 见默认配置 |

---

## 使用方法

### AI 智能体模式（推荐）

直接在聊天中发送自然语言，AI 会自动判断并调用绘图/视频工具：

```
画一个赛博朋克风的中国女孩
[发图] 帮我把这张照片变成吉卜力风格
生成一段雨中飞驰的竖屏视频
[发图] 让照片里的人跳舞，要竖屏
```

### 硬指令模式

| 指令 | 说明 | 示例 |
|------|------|------|
| `gg文 <描述>` | 文生图 | `gg文 飞在天上的汽车` |
| `gg图 <描述>` + 附图 | 图生图 | `gg图 变成水彩画` |
| `gg片 [描述]` + 附图 | 图生视频 | `gg片 让她跳舞` |
| `gg动 <描述>` | 文生视频 | `gg动 赛博朋克城市夜景` |
| `<咒语词>` | 快捷触发预设 Prompt | `手办化` |

### 系统管理指令

```
gg模型列表      # 查看所有可用模型
gg切换模型      # 循环切换当前绘图引擎
gg设置面板      # 查看当前配置状态
gg绘画帮助      # 显示帮助信息
```

---

## 支持模型

**绘图模型**
- `gemini-2.5-flash-image-landscape / portrait`
- `gemini-3.0-pro-image-landscape / portrait`
- `imagen-4.0-generate-preview-landscape / portrait`

**视频模型（自动切换）**
- `veo_3_1_t2v_fast_landscape / portrait`（文生视频）
- `veo_3_1_r2v_fast / portrait`（图生视频）

---

## 依赖

- Python 3.9+
- `aiohttp`
- `Pillow`（强烈建议安装，缺失时图片处理能力降级）

---

## License

MIT © [XIAOKU2300](https://github.com/XIAOKU2300/astrbot_plugin_Bettergemini)
