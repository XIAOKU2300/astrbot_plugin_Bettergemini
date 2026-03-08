import aiohttp
import asyncio
import json
import re
import time
import base64
import io
import os
from typing import Tuple, Optional, Dict

from astrbot.api.message_components import *
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# 强制要求安装 Pillow 处理图片
try:
    from PIL import Image as PyImage, UnidentifiedImageError
except ImportError:
    PyImage = None
    logger.error("❌ 未安装 Pillow 库！图片处理功能将受到严重限制。请执行: pip install Pillow")

@register("gemini-draw", "Flow2API", "基于 Flow2API 的全能视觉插件 (V9.9 纯净重构版)", "9.9")
class Flow2APIDrawPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        
        self.api_url = config.get("api_url", "http://127.0.0.1:8000/v1/chat/completions")
        self.apikey = config.get("apikey", "")
        if not self.apikey:
            logger.warning("⚠️ 警告: 未配置 API Key，插件无法正常调用接口。")

        self.available_models = [
            "gemini-2.5-flash-image-landscape",
            "gemini-2.5-flash-image-portrait",
            "gemini-3.0-pro-image-landscape",
            "gemini-3.0-pro-image-portrait",
            "imagen-4.0-generate-preview-landscape",
            "imagen-4.0-generate-preview-portrait"
        ]
        
        custom_model = str(config.get("custom_model", "")).strip()
        if custom_model and custom_model not in self.available_models:
            self.available_models.append(custom_model)

        default_model = str(config.get("model", "imagen-4.0-generate-preview-landscape")).strip()
        self.current_model = default_model if default_model in self.available_models else self.available_models[0]

        # 自动叠加的审美预设词
        self.aesthetic_tags = config.get(
            "aesthetic_tags", 
            "masterpiece, best quality, Asian aesthetic, delicate and beautiful facial features, clear and soft lighting, highly detailed, cinematic"
        )

        self.prompt_map: Dict[str, str] = {}
        self._load_prompt_map(config)
        
        logger.info(f"✅ Flow2API 插件加载成功 | 当前绘图模型: {self.current_model}")

    def _load_prompt_map(self, config: dict):
        prompt_list = config.get("prompt_list", [])
        for item in prompt_list:
            if ":" in item:
                key, value = item.split(":", 1)
                self.prompt_map[key.strip()] = value.strip()

    # ==========================================
    # 核心一：兼容 HTTP 与 本地路径的图片处理器
    # ==========================================
    async def _download_and_process_image(self, img_url: str, max_size: int = 1536) -> Optional[str]:
        if img_url.startswith("data:image/"): return img_url

        img_data = None
        try:
            if img_url.startswith("http"):
                headers = {"User-Agent": "Mozilla/5.0"}
                timeout = aiohttp.ClientTimeout(total=20)
                proxy = os.environ.get("all_proxy") or os.environ.get("ALL_PROXY") or \
                        os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY") or \
                        os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
                
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(img_url, headers=headers, proxy=proxy) as resp:
                        if resp.status == 200:
                            img_data = await resp.read()
            elif os.path.exists(img_url) or img_url.startswith("file://"):
                path = img_url.replace("file://", "")
                with open(path, "rb") as f:
                    img_data = f.read()
        except Exception as e:
            logger.error(f"❌ 下载图片异常: {e}")
            return None

        if not img_data: return None

        if not PyImage:
            return f"data:image/jpeg;base64,{base64.b64encode(img_data).decode('utf-8')}"

        try:
            img = PyImage.open(io.BytesIO(img_data))
            if getattr(img, "is_animated", False): img.seek(0)
            img = img.convert("RGB")
            if img.width > max_size or img.height > max_size:
                img.thumbnail((max_size, max_size), PyImage.Resampling.LANCZOS)
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=85)
            b64_data = base64.b64encode(buffer.getvalue()).decode('utf-8')
            return f"data:image/jpeg;base64,{b64_data}"
        except Exception:
            return None

    def _resize_base64(self, b64_str: str, scale: float = 0.7) -> str:
        if not b64_str or not PyImage: return b64_str
        try:
            header, data = b64_str.split(",", 1) if "," in b64_str else ("data:image/jpeg;base64,", b64_str)
            if not header.endswith(","): header += ","
            img = PyImage.open(io.BytesIO(base64.b64decode(data))).convert("RGB")
            new_size = (int(img.width * scale), int(img.height * scale))
            if new_size[0] < 256: return b64_str
            img = img.resize(new_size, PyImage.Resampling.LANCZOS)
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=75)
            return f"{header}{base64.b64encode(buffer.getvalue()).decode('utf-8')}"
        except Exception:
            return b64_str

    async def _extract_image_from_event(self, event: AstrMessageEvent) -> Optional[str]:
        messages = event.get_messages()
        for seg in messages:
            if isinstance(seg, Image):
                if hasattr(seg, 'url') and seg.url: return seg.url
                if hasattr(seg, 'file') and seg.file:
                    if str(seg.file).startswith("http"): return seg.file
                    if os.path.exists(str(seg.file)): return str(seg.file)
                if hasattr(seg, 'path') and seg.path and os.path.exists(str(seg.path)): return str(seg.path)
                if hasattr(seg, 'base64') and seg.base64: return f"data:image/jpeg;base64,{seg.base64}"
            if isinstance(seg, At) and hasattr(seg, 'qq'):
                return f"https://q.qlogo.cn/g?b=qq&nk={seg.qq}&s=640"
            if isinstance(seg, Reply):
                chain = getattr(seg, 'chain', []) or getattr(seg, 'message', [])
                for reply_seg in chain:
                    if isinstance(reply_seg, Image):
                        if hasattr(reply_seg, 'url') and reply_seg.url: return reply_seg.url
                        if hasattr(reply_seg, 'file') and reply_seg.file:
                            if str(reply_seg.file).startswith("http"): return reply_seg.file
                            if os.path.exists(str(reply_seg.file)): return str(reply_seg.file)
                        if hasattr(reply_seg, 'base64') and reply_seg.base64: return f"data:image/jpeg;base64,{reply_seg.base64}"
        return None

    # ==========================================
    # 核心三：请求引擎
    # ==========================================
    async def _generate_media(self, prompt: str, image_b64: Optional[str] = None, override_model: str = None) -> Tuple[bool, str]:
        headers = {
            'Authorization': f'Bearer {self.apikey}',
            'Content-Type': 'application/json'
        }
        
        current_b64 = image_b64
        max_retries = 3

        for attempt in range(max_retries + 1):
            is_retry = attempt > 0
            if is_retry and current_b64:
                current_b64 = self._resize_base64(current_b64, 0.75)

            content_list = [{"type": "text", "text": prompt}]
            if current_b64:
                content_list.append({
                    "type": "image_url",
                    "image_url": {
                        "url": current_b64,
                        "detail": "low" if is_retry else "high"
                    }
                })

            payload = {
                "model": override_model or self.current_model,
                "messages": [{"role": "user", "content": content_list}],
                "stream": True 
            }

            try:
                timeout = aiohttp.ClientTimeout(total=300)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(self.api_url, json=payload, headers=headers) as resp:
                        if resp.status != 200:
                            err_txt = await resp.text()
                            if "UNSAFE_GENERATION" in err_txt or "safety" in err_txt.lower():
                                return False, "🛑 提示词触发了模型安全审核过滤机制。"
                            if attempt < max_retries:
                                await asyncio.sleep(2 ** attempt)
                                continue
                            return False, f"API 异常 [HTTP {resp.status}]"

                        full_content = ""
                        raw_text = ""
                        async for line in resp.content:
                            line_str = line.decode('utf-8').strip()
                            if not line_str: continue
                            raw_text += line_str + "\n"
                            if line_str == "data: [DONE]": continue
                            if line_str.startswith("data: "):
                                try:
                                    chunk = json.loads(line_str[6:])
                                    choices = chunk.get("choices", [])
                                    if choices and isinstance(choices, list) and len(choices) > 0:
                                        delta_content = choices[0].get("delta", {}).get("content", "")
                                        if delta_content: full_content += delta_content
                                except Exception: pass

                        if not full_content and raw_text.startswith("{"):
                            try:
                                err_json = json.loads(raw_text)
                                if "error" in err_json:
                                    err_msg = str(err_json.get("error"))
                                    if "UNSAFE_GENERATION" in err_msg or "safety" in err_msg.lower():
                                        return False, "🛑 提示词触发了安全审核过滤机制。"
                                    return False, f"⚠️ API 拒绝请求: {err_msg[:50]}"
                            except Exception: pass

                        if "UNSAFE_GENERATION" in full_content or "UNSAFE_GENERATION" in raw_text:
                            return False, "🛑 提示词触发了模型安全审核过滤机制。"

                        url_match = re.search(r'(https?://[^\s<>"\']+)', full_content)
                        if url_match:
                            return True, url_match.group(1).rstrip(')]}')
                        else:
                            return False, f"未生成多媒体链接。模型反馈如下:\n{full_content[:100]}"

            except asyncio.TimeoutError:
                if attempt == max_retries: return False, "⏳ 服务器响应超时，可能渲染任务正在排队。"
            except Exception as e:
                if attempt == max_retries: return False, f"内部网络崩溃: {str(e)}"

            await asyncio.sleep(2 ** attempt)

        return False, "重试次数已耗尽，生成失败。"


    # ==========================================
    # 🌟 核心四：大模型原生 Tool 智能调用 (纯净修复版)
    # ==========================================
    @filter.llm_tool(name="draw_image")
    async def draw_image_tool(self, event: AstrMessageEvent, prompt: str):
        '''调用AI绘画引擎生成图片。包含文生图和图生图。当用户要求画画、生成图片、转换图片风格时调用此工具。
        Args:
            prompt(string): 图片的纯英文详细描述。如果是基于原图修改，请描述原图的核心内容并加上风格词。系统会自动叠加亚洲审美倾向，你只需专注画面核心描述。
        '''
        if not self.apikey:
            yield event.plain_result("❌ 系统提示：未配置API Key，请联系管理员。")
            return

        raw_url = await self._extract_image_from_event(event)
        
        final_prompt = prompt
        if self.aesthetic_tags:
            final_prompt = f"{prompt}, {self.aesthetic_tags}"

        if raw_url:
            yield event.plain_result("🎨 收到！已成功捕获到您的原图，正在为您进行【图生图】艺术重绘，请稍等...")
            b64_data = await self._download_and_process_image(raw_url)
            if not b64_data:
                yield event.plain_result("❌ 系统提示：原图片下载/读取失败，无法完成图生图。")
                return
            success, result = await self._generate_media(final_prompt, b64_data)
        else:
            yield event.plain_result("🎨 收到！正在为您进行纯文本的【文生图】创作，请稍等...")
            success, result = await self._generate_media(final_prompt, None)

        if success:
            yield event.chain_result([Image.fromURL(result)])
        else:
            yield event.plain_result(f"💥 生成失败了：{result}")

    @filter.llm_tool(name="generate_video")
    async def generate_video_tool(self, event: AstrMessageEvent, prompt: str, is_portrait: bool):
        '''调用AI视频引擎生成动态视频。包含文生视频和图生视频。
        Args:
            prompt(string): 用纯英文详细描述视频的动态过程和画面细节。
            is_portrait(boolean): 用户是否提到需要竖屏、手机观看等（是则true，否则false）。
        '''
        if not self.apikey:
            yield event.plain_result("❌ 系统提示：未配置API Key，请联系管理员。")
            return

        raw_url = await self._extract_image_from_event(event)
        
        final_prompt = prompt
        if self.aesthetic_tags:
            final_prompt = f"{prompt}, {self.aesthetic_tags}"
            
        if raw_url:
            target_model = "veo_3_1_r2v_fast_portrait" if is_portrait else "veo_3_1_r2v_fast"
            yield event.plain_result(f"🎬 收到！已捕获原图，正在进行【图生视频】渲染 ({target_model})，需要一定时间...")
            b64_data = await self._download_and_process_image(raw_url)
            if not b64_data:
                yield event.plain_result("❌ 系统提示：图片读取失败，请检查图片网络状态。")
                return
        else:
            target_model = "veo_3_1_t2v_fast_portrait" if is_portrait else "veo_3_1_t2v_fast_landscape"
            yield event.plain_result(f"🎬 收到！正在进行【文生视频】大片渲染 ({target_model})，可能需要一点点时间...")
            b64_data = None

        success, result = await self._generate_media(final_prompt, b64_data, override_model=target_model)

        if success:
            yield event.plain_result(f"🔗 专属视频链接 (如无法直接播放请点开):\n{result}")
        else:
            yield event.plain_result(f"💥 视频生成失败：{result}")


    # ==========================================
    # 核心五：传统硬指令拦截器
    # ==========================================
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_draw_command(self, event: AstrMessageEvent):
        pure_text = ""
        for seg in event.get_messages():
            if isinstance(seg, Plain): pure_text += seg.text + " "
        
        pure_text = pure_text.strip().lstrip("/")
        if not pure_text: return

        cmd = ""
        prompt = ""

        if pure_text.startswith("gg文"):
            cmd = "gg文"
            prompt = pure_text[len("gg文"):].strip()
        elif pure_text.startswith("gg图"):
            cmd = "gg图"
            prompt = pure_text[len("gg图"):].strip()
        elif pure_text.startswith("gg片"):
            cmd = "gg片"
            prompt = pure_text[len("gg片"):].strip()
        elif pure_text.startswith("gg动"):
            cmd = "gg动"
            prompt = pure_text[len("gg动"):].strip()
        else:
            first_word = pure_text.split()[0]
            if first_word in self.prompt_map:
                cmd = "shortcut"
                prompt = self.prompt_map[first_word]
        
        if not cmd: return
        if hasattr(event, "stop_event"): event.stop_event()

        if not self.apikey:
            yield event.plain_result("❌ 管理员尚未配置 API Key。")
            return

        if cmd == "gg文":
            async for res in self._handle_t2i(event, prompt): yield res
        elif cmd == "gg图":
            async for res in self._handle_i2i(event, prompt): yield res
        elif cmd == "gg片":
            async for res in self._handle_i2v(event, prompt): yield res
        elif cmd == "gg动":
            async for res in self._handle_t2v(event, prompt): yield res
        elif cmd == "shortcut":
            async for res in self._handle_shortcut(event, prompt, first_word): yield res

    # ==========================================
    # 路由任务处理器 (供手动硬指令使用)
    # ==========================================
    async def _handle_t2i(self, event: AstrMessageEvent, prompt: str):
        if not prompt:
            yield event.plain_result("⚠️ 格式错误！示例：gg文 飞在天上的汽车")
            return
        yield event.plain_result(f"🎨 [硬指令·文生图] 执行中...")
        final_prompt = f"{prompt}, {self.aesthetic_tags}" if self.aesthetic_tags else prompt
        success, result = await self._generate_media(final_prompt, None)
        if success: yield event.chain_result([Image.fromURL(result)])
        else: yield event.plain_result(f"💥 生成失败\n原因: {result}")

    async def _handle_i2i(self, event: AstrMessageEvent, prompt: str):
        if not prompt:
            yield event.plain_result("⚠️ 格式错误！示例：gg图 变成水彩画")
            return
        raw_url = await self._extract_image_from_event(event)
        if not raw_url:
            yield event.plain_result("❌ 找不到图片！请附带/引用图片。")
            return
        yield event.plain_result(f"📡 [硬指令·图生图] 处理中...")
        b64_data = await self._download_and_process_image(raw_url)
        if not b64_data:
            yield event.plain_result("❌ 图片读取失败。")
            return
        final_prompt = f"{prompt}, {self.aesthetic_tags}" if self.aesthetic_tags else prompt
        success, result = await self._generate_media(final_prompt, b64_data)
        if success: yield event.chain_result([Image.fromURL(result)])
        else: yield event.plain_result(f"💥 转化失败\n原因: {result}")

    async def _handle_i2v(self, event: AstrMessageEvent, prompt: str):
        prompt = prompt or "转换为高质量动态视频"
        raw_url = await self._extract_image_from_event(event)
        if not raw_url:
            yield event.plain_result("❌ 找不到图片！图生片需附带图片。")
            return
        target_model = "veo_3_1_r2v_fast_portrait" if "竖" in prompt else "veo_3_1_r2v_fast"
        yield event.plain_result(f"🎬 [硬指令·图生片] 正在提交任务...\n引擎: {target_model}")
        b64_data = await self._download_and_process_image(raw_url)
        if not b64_data:
            yield event.plain_result("❌ 图片读取失败。")
            return
        final_prompt = f"{prompt}, {self.aesthetic_tags}" if self.aesthetic_tags else prompt
        success, result = await self._generate_media(final_prompt, b64_data, override_model=target_model)
        if success: yield event.plain_result(f"🎉 视频完成:\n{result}")
        else: yield event.plain_result(f"💥 渲染失败\n原因: {result}")

    async def _handle_t2v(self, event: AstrMessageEvent, prompt: str):
        if not prompt:
            yield event.plain_result("⚠️ 格式错误！示例：gg动 赛博朋克城市")
            return
        target_model = "veo_3_1_t2v_fast_portrait" if "竖" in prompt else "veo_3_1_t2v_fast_landscape"
        yield event.plain_result(f"🎬 [硬指令·文生片] 提交中...\n引擎: {target_model}")
        final_prompt = f"{prompt}, {self.aesthetic_tags}" if self.aesthetic_tags else prompt
        success, result = await self._generate_media(final_prompt, None, override_model=target_model)
        if success: yield event.plain_result(f"🎉 视频完成:\n{result}")
        else: yield event.plain_result(f"💥 渲染失败\n原因: {result}")

    async def _handle_shortcut(self, event: AstrMessageEvent, prompt: str, cmd_name: str):
        raw_url = await self._extract_image_from_event(event)
        b64_data = None
        if raw_url:
            yield event.plain_result(f"🛠️ 触发图生图快捷技: [{cmd_name}]...")
            b64_data = await self._download_and_process_image(raw_url)
        else:
            yield event.plain_result(f"🛠️ 触发文生图快捷技: [{cmd_name}]...")
        final_prompt = f"{prompt}, {self.aesthetic_tags}" if self.aesthetic_tags else prompt
        success, result = await self._generate_media(final_prompt, b64_data)
        if success: yield event.chain_result([Image.fromURL(result)])
        else: yield event.plain_result(f"💥 执行失败\n{result}")

    # ==========================================
    # 系统与模型管理指令
    # ==========================================
    @filter.command("gg绘画帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        msg = (
            "🎨 Flow2API 视觉系统 (V9.9 最终稳定版)\n"
            "━━━━━━━━━━━━━━━━\n"
            "🤖 【AI 智能体画画 (推荐)】\n"
            "直接带图或者直接发话：\n"
            "• “画一张赛博朋克风的中国女孩”\n"
            "• [带图]“帮我把照片转成吉卜力风格”\n"
            "• “生成一段雨中飞驰的竖屏视频”\n"
            "• [带图]“让照片里的人动起来”\n\n"
            "📖 【老玩家硬指令】\n"
            "`gg文` | `gg图` | `gg片` | `gg动`\n\n"
            "🔧 【系统设置】\n"
            "`gg模型列表` | `gg切换模型` | `gg设置面板`"
        )
        yield event.plain_result(msg)

    @filter.command("gg切换模型")
    async def switch_model(self, event: AstrMessageEvent):
        idx = self.available_models.index(self.current_model) if self.current_model in self.available_models else 0
        self.current_model = self.available_models[(idx + 1) % len(self.available_models)]
        yield event.plain_result(f"🔄 绘图引擎已切换至:\n{self.current_model}")

    @filter.command("gg模型列表")
    async def list_models(self, event: AstrMessageEvent):
        msg_parts = [f"📚 绘图引擎库 (共{len(self.available_models)}个):"]
        for i, m in enumerate(self.available_models, 1):
            mark = " 👈 [当前]" if m == self.current_model else ""
            msg_parts.append(f"{i}. {m}{mark}")
        yield event.plain_result("\n".join(msg_parts))

    @filter.command("gg设置面板")
    async def show_settings(self, event: AstrMessageEvent):
        key_mask = f"{self.apikey[:6]}****{self.apikey[-4:]}" if (self.apikey and len(self.apikey) > 10) else "未配置/过短"
        yield event.plain_result(
            f"⚙️ 插件状态面板 v9.9\n━━━━━━━━━━━━━━━━━\n"
            f"🔗 节点: {self.api_url}\n"
            f"🔑 密钥: {key_mask}\n"
            f"🤖 绘图引擎: {self.current_model}\n"
            f"💄 默认审美: 亚洲高级审美已装载\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"纯净智能体模式稳定运行中"
        )