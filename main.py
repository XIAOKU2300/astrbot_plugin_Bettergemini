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

@register("gemini-draw", "Flow2API", "基于 Flow2API 的全能绘图/视频插件 (gg终极防漏版)", "9.3")
class Flow2APIDrawPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        
        # 1. 基础 API 配置
        self.api_url = config.get("api_url", "http://127.0.0.1:8000/v1/chat/completions")
        self.apikey = config.get("apikey", "")
        if not self.apikey:
            logger.warning("⚠️ 警告: 未配置 API Key，插件无法正常调用接口。")

        # 2. 模型矩阵配置
        self.available_models = [
            "gemini-2.5-flash-image-landscape",
            "gemini-2.5-flash-image-portrait",
            "gemini-3.0-pro-image-landscape",
            "gemini-3.0-pro-image-portrait",
            "imagen-4.0-generate-preview-landscape",
            "imagen-4.0-generate-preview-portrait",
            "video-generation-model" # 占位视频模型
        ]
        
        # 加载自定义模型
        custom_model = str(config.get("custom_model", "")).strip()
        if custom_model and custom_model not in self.available_models:
            self.available_models.append(custom_model)

        # 确认当前运行模型
        default_model = str(config.get("model", "imagen-4.0-generate-preview-landscape")).strip()
        self.current_model = default_model if default_model in self.available_models else self.available_models[0]

        # 3. 快捷提示词映射库
        self.prompt_map: Dict[str, str] = {}
        self._load_prompt_map(config)
        
        # 4. 初始化全局 Session 占位符
        self._session: Optional[aiohttp.ClientSession] = None
        
        logger.info(f"✅ Flow2API 插件加载成功 | 当前模型: {self.current_model}")

    def _load_prompt_map(self, config: dict):
        prompt_list = config.get("prompt_list", [])
        for item in prompt_list:
            if ":" in item:
                key, value = item.split(":", 1)
                self.prompt_map[key.strip()] = value.strip()

    # ==========================================
    # 网络会话管理
    # ==========================================
    async def get_session(self) -> aiohttp.ClientSession:
        """获取或创建全局复用的 ClientSession"""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=180)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def terminate(self):
        """AstrBot 卸载插件时自动调用的清理方法"""
        if self._session and not self._session.closed:
            await self._session.close()

    # ==========================================
    # 纯 CPU 密集型任务 (用于交由后台线程执行)
    # ==========================================
    @staticmethod
    def _process_image_sync(img_data: bytes, max_size: int = 1536) -> Optional[str]:
        """同步的图片处理逻辑 (压缩与 Base64 编码)"""
        if not PyImage:
            return f"data:image/jpeg;base64,{base64.b64encode(img_data).decode('utf-8')}"

        try:
            img = PyImage.open(io.BytesIO(img_data))
            if getattr(img, "is_animated", False):
                img.seek(0)
                
            img = img.convert("RGB")
            if img.width > max_size or img.height > max_size:
                img.thumbnail((max_size, max_size), PyImage.Resampling.LANCZOS)
                
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=85)
            b64_data = base64.b64encode(buffer.getvalue()).decode('utf-8')
            return f"data:image/jpeg;base64,{b64_data}"
        except UnidentifiedImageError:
            logger.error("❌ 无法识别图片内容，可能是文件已损坏。")
            return None
        except Exception as e:
            logger.error(f"❌ Pillow 处理崩溃: {e}")
            return None

    @staticmethod
    def _resize_base64_sync(b64_str: str, scale: float = 0.7) -> str:
        """同步的 Base64 图片缩放逻辑"""
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
        except Exception as e:
            logger.warning(f"⚠️ 图片二次压缩失败: {e}")
            return b64_str

    # ==========================================
    # 核心一：兼容 HTTP 与 本地路径的图片处理器
    # ==========================================
    async def _download_and_process_image(self, img_url: str, max_size: int = 1536) -> Optional[str]:
        if img_url.startswith("data:image/"):
            return img_url

        img_data = None
        try:
            if img_url.startswith("http"):
                logger.info(f"⬇️ 正在拉取网络图片: {img_url[:60]}...")
                headers = {"User-Agent": "Mozilla/5.0"}
                session = await self.get_session()
                # 针对单个请求临时覆盖超时时间 (图片下载不需要 180s)
                async with session.get(img_url, headers=headers, timeout=20) as resp:
                    if resp.status != 200:
                        logger.error(f"❌ 图片下载失败 HTTP {resp.status}")
                        return None
                    img_data = await resp.read()
            elif os.path.exists(img_url) or img_url.startswith("file://"):
                path = img_url.replace("file://", "")
                logger.info(f"⬇️ 正在读取本地图片: {path}")
                # 文件 IO 也推荐放入线程以防阻塞
                def read_file(p):
                    with open(p, "rb") as f: return f.read()
                img_data = await asyncio.to_thread(read_file, path)
            else:
                logger.error(f"❌ 无法识别的图片路径格式: {img_url}")
                return None
        except Exception as e:
            logger.error(f"❌ 获取图片源数据失败: {e}")
            return None

        if not img_data:
            return None

        # 将 CPU 密集的图片处理任务交由后台线程执行
        return await asyncio.to_thread(self._process_image_sync, img_data, max_size)

    # ==========================================
    # 核心二：深度事件提取器 (完全覆盖本地与网络)
    # ==========================================
    def _get_image_url_from_seg(self, seg) -> Optional[str]:
        """辅助方法：从单个消息段中提取图片 URL 或路径"""
        if isinstance(seg, Image):
            if hasattr(seg, 'url') and seg.url: return seg.url
            if hasattr(seg, 'file') and seg.file:
                if str(seg.file).startswith("http"): return seg.file
                if os.path.exists(str(seg.file)): return str(seg.file)
            if hasattr(seg, 'path') and seg.path and os.path.exists(str(seg.path)): return str(seg.path)
            if hasattr(seg, 'base64') and seg.base64: return f"data:image/jpeg;base64,{seg.base64}"
        return None

    async def _extract_image_from_event(self, event: AstrMessageEvent) -> Optional[str]:
        messages = event.get_messages()
        for seg in messages:
            # 1. 检查直接附带的图片
            img_url = self._get_image_url_from_seg(seg)
            if img_url: return img_url
            
            # 2. 检查 At 对象获取头像
            if isinstance(seg, At) and hasattr(seg, 'qq'):
                return f"https://q.qlogo.cn/g?b=qq&nk={seg.qq}&s=640"
                
            # 3. 检查回复中的图片
            if isinstance(seg, Reply):
                chain = getattr(seg, 'chain', []) or getattr(seg, 'message', [])
                for reply_seg in chain:
                    reply_img_url = self._get_image_url_from_seg(reply_seg)
                    if reply_img_url: return reply_img_url
        return None

    # ==========================================
    # 核心三：请求引擎
    # ==========================================
    async def _generate_media(self, prompt: str, image_b64: Optional[str] = None) -> Tuple[bool, str]:
        headers = {
            'Authorization': f'Bearer {self.apikey}',
            'Content-Type': 'application/json'
        }
        
        current_b64 = image_b64
        max_retries = 3

        for attempt in range(max_retries + 1):
            is_retry = attempt > 0
            
            if is_retry and current_b64:
                logger.warning(f"🔄 第 {attempt} 次重试：正在压缩图片负载...")
                # 将 Base64 压缩交由后台线程
                current_b64 = await asyncio.to_thread(self._resize_base64_sync, current_b64, 0.75)

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
                "model": self.current_model,
                "messages": [{"role": "user", "content": content_list}],
                "stream": True 
            }

            try:
                session = await self.get_session()
                async with session.post(self.api_url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        err_txt = await resp.text()
                        # 仅针对特定的状态码进行重试，如 429(限流) 或 5xx(服务端错误)
                        if resp.status in [429, 500, 502, 503, 504] and attempt < max_retries:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        # 对于 400 等客户端错误，直接返回不重试
                        return False, f"API 异常 [HTTP {resp.status}]\n详情: {err_txt[:150]}"

                    full_content = ""
                    async for line in resp.content:
                        line_str = line.decode('utf-8').strip()
                        if not line_str or line_str == "data: [DONE]":
                            continue
                        if line_str.startswith("data: "):
                            try:
                                chunk_data = line_str[6:]
                                if not chunk_data: continue
                                chunk = json.loads(chunk_data)
                                choices = chunk.get("choices", [])
                                if choices and isinstance(choices, list) and len(choices) > 0:
                                    delta_content = choices[0].get("delta", {}).get("content", "")
                                    if delta_content:
                                        full_content += delta_content
                            except Exception as e:
                                logger.warning(f"⚠️ JSON 流解析失败: {e} | 数据块: {chunk_data[:50]}")

                    # 提取链接，并修复正则可能会带入末尾标点符号的问题
                    url_match = re.search(r'(https?://[^\s<>"\']+)', full_content)
                    if url_match:
                        raw_url = url_match.group(1).rstrip(')]}')
                        # 剔除末尾可能带有的中文/英文句号等标点
                        raw_url = re.sub(r'[.,;!?。，！？]$', '', raw_url)
                        return True, raw_url
                    else:
                        return False, f"未检测到多媒体链接，返回文本为:\n{full_content[:200]}..."

            except asyncio.TimeoutError:
                if attempt == max_retries: return False, "服务器响应超时 (180s)，请稍后再试。"
            except Exception as e:
                if attempt == max_retries: return False, f"内部网络崩溃: {str(e)}"

            await asyncio.sleep(2 ** attempt)

        return False, "重试次数已耗尽，生成失败。"

    # ==========================================
    # 核心四：终极拦截器 (无视排版，100%抓取)
    # ==========================================
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_draw_command(self, event: AstrMessageEvent):
        """抛弃脆弱的 filter.command，采用最高优先级全局拦截，解决首部图片导致解析失败的问题"""
        # 1. 深度洗出纯文本内容
        pure_text = ""
        for seg in event.get_messages():
            if isinstance(seg, Plain):
                pure_text += seg.text + " "
        
        # 清除首尾空白和意外前缀
        pure_text = pure_text.strip().lstrip("/")
        if not pure_text:
            return

        cmd = ""
        prompt = ""

        # 2. 匹配业务逻辑指令
        if pure_text.startswith("gg文"):
            cmd = "gg文"
            prompt = pure_text[len("gg文"):].strip()
        elif pure_text.startswith("gg图"):
            cmd = "gg图"
            prompt = pure_text[len("gg图"):].strip()
        elif pure_text.startswith("gg片"):
            cmd = "gg片"
            prompt = pure_text[len("gg片"):].strip()
        else:
            # 检查是否为配置中的快捷咒语
            first_word = pure_text.split()[0]
            if first_word in self.prompt_map:
                cmd = "shortcut"
                prompt = self.prompt_map[first_word]
        
        # 没匹配到直接放行给大模型或其他插件
        if not cmd:
            return

        # 🔥 关键：匹配到了立即截断事件流
        if hasattr(event, "stop_event"):
            event.stop_event()

        if not self.apikey:
            yield event.plain_result("❌ 管理员尚未配置 API Key。")
            return

        # 3. 路由任务
        if cmd == "gg文":
            async for res in self._handle_t2i(event, prompt): yield res
        elif cmd == "gg图":
            async for res in self._handle_i2i(event, prompt): yield res
        elif cmd == "gg片":
            async for res in self._handle_i2v(event, prompt): yield res
        elif cmd == "shortcut":
            async for res in self._handle_shortcut(event, prompt, first_word): yield res

    # ==========================================
    # 路由任务处理器
    # ==========================================
    async def _handle_t2i(self, event: AstrMessageEvent, prompt: str):
        if not prompt:
            yield event.plain_result("⚠️ 格式错误！请输入你要画的内容。\n示例：gg文 飞在天上的汽车")
            return
        t_start = time.time()
        yield event.plain_result(f"🎨 [文生图] 正在构思: {prompt[:30]}...")

        success, result = await self._generate_media(prompt, None)
        t_cost = time.time() - t_start

        if success:
            yield event.chain_result([Plain(f"✨ 绘制完毕！(耗时: {t_cost:.1f}s)\n"), Image.fromURL(result)])
        else:
            yield event.plain_result(f"💥 生成失败 (耗时 {t_cost:.1f}s)\n原因: {result}")

    async def _handle_i2i(self, event: AstrMessageEvent, prompt: str):
        if not prompt:
            yield event.plain_result("⚠️ 格式错误！请输入提示词并附带图片。\n示例：gg图 变成水彩画")
            return
        t_start = time.time()
        raw_url = await self._extract_image_from_event(event)
        if not raw_url:
            yield event.plain_result("❌ 找不到图片！请确保【图片】和【指令】在同一条消息发送，或者【引用】带有图片的消息。")
            return

        yield event.plain_result(f"📡 [图生图] 已捕获图片，处理中...\n🎨 提示词: {prompt[:30]}...")
        b64_data = await self._download_and_process_image(raw_url)
        if not b64_data:
            yield event.plain_result("❌ 图片读取失败。可能是本地读取权限不足或网络图片已失效。")
            return

        success, result = await self._generate_media(prompt, b64_data)
        t_cost = time.time() - t_start

        if success:
            yield event.chain_result([Plain(f"✨ 魔法转化完毕！(耗时: {t_cost:.1f}s)\n"), Image.fromURL(result)])
        else:
            yield event.plain_result(f"💥 转化失败 (耗时 {t_cost:.1f}s)\n原因: {result}")

    async def _handle_i2v(self, event: AstrMessageEvent, prompt: str):
        if not prompt:
            prompt = "请将这张图片转换为高质量动态视频，保持原图的主题、光影和逻辑。"
        t_start = time.time()
        raw_url = await self._extract_image_from_event(event)
        if not raw_url:
            yield event.plain_result("❌ 找不到原始图片！图生片必须附带/引用一张图片。")
            return

        yield event.plain_result(f"🎬 [图生片] 已捕获图片，正在提交视频渲染任务 (耗时较长)...\n📝 提示词: {prompt[:20]}...")
        b64_data = await self._download_and_process_image(raw_url)
        if not b64_data:
            yield event.plain_result("❌ 图片读取失败，请检查文件状态。")
            return

        success, result = await self._generate_media(prompt, b64_data)
        t_cost = time.time() - t_start

        if success:
            yield event.plain_result(f"🎉 视频渲染完成！(耗时: {t_cost:.1f}s)\n🔗 直达链接 (如无法直接播放请点开链接):\n{result}")
        else:
            yield event.plain_result(f"💥 视频渲染失败 (耗时 {t_cost:.1f}s)\n原因: {result}")

    async def _handle_shortcut(self, event: AstrMessageEvent, prompt: str, cmd_name: str):
        t_start = time.time()
        raw_url = await self._extract_image_from_event(event)
        b64_data = None
        if raw_url:
            yield event.plain_result(f"🛠️ 触发图生图快捷技: [{cmd_name}]...")
            b64_data = await self._download_and_process_image(raw_url)
        else:
            yield event.plain_result(f"🛠️ 触发文生图快捷技: [{cmd_name}]...")

        success, result = await self._generate_media(prompt, b64_data)
        t_cost = time.time() - t_start

        if success:
            yield event.chain_result([Plain(f"✨ 指令执行完毕 (耗时: {t_cost:.1f}s)\n"), Image.fromURL(result)])
        else:
            yield event.plain_result(f"💥 快捷指令执行失败\n{result}")

    # ==========================================
    # 系统与模型管理指令（无需改动，纯文本支持）
    # ==========================================
    @filter.command("gg绘画帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        msg = (
            "🎨 Flow2API 智能绘图系统 (gg终极防漏版)\n"
            "━━━━━━━━━━━━━━━━\n"
            "📖 【指令列表】\n"
            "1️⃣ 文生图：\n"
            "用法：发送 `gg文 <描述词>`\n"
            "示例：gg文 一只赛博朋克风格的柴犬\n\n"
            "2️⃣ 图生图：\n"
            "用法：发送 `gg图 <描述词>` 并附带图片 (或引用某张图)\n"
            "示例：gg图 变成二次元动漫风格\n\n"
            "3️⃣ 图生片 (视频)：\n"
            "用法：发送 `gg片 [描述词]` 并附带图片\n"
            "示例：gg片 让这张照片里的人物动起来\n\n"
            "🔧 【系统指令】\n"
            "`gg模型列表` - 查看可用引擎\n"
            "`gg切换模型` - 顺序切换模型\n"
            "`gg设置面板` - 查看系统状态"
        )
        yield event.plain_result(msg)

    @filter.command("gg切换模型")
    async def switch_model(self, event: AstrMessageEvent):
        idx = self.available_models.index(self.current_model) if self.current_model in self.available_models else 0
        self.current_model = self.available_models[(idx + 1) % len(self.available_models)]
        yield event.plain_result(f"🔄 模型引擎已切换至:\n{self.current_model}\n\n💡 提示: 输入 `gg模型列表` 可查看全部。")

    @filter.command("gg模型列表")
    async def list_models(self, event: AstrMessageEvent):
        msg_parts = [f"📚 Flow2API 引擎库 (共{len(self.available_models)}个):"]
        for i, m in enumerate(self.available_models, 1):
            mark = " 👈 [当前使用]" if m == self.current_model else ""
            msg_parts.append(f"{i}. {m}{mark}")
        yield event.plain_result("\n".join(msg_parts))

    @filter.command("gg设置面板")
    async def show_settings(self, event: AstrMessageEvent):
        key_mask = f"{self.apikey[:6]}****{self.apikey[-4:]}" if (self.apikey and len(self.apikey) > 10) else "未配置/过短"
        yield event.plain_result(
            f"⚙️ 插件状态面板 v9.3\n━━━━━━━━━━━━━━━━━\n"
            f"🔗 节点: {self.api_url}\n"
            f"🔑 密钥: {key_mask}\n"
            f"🤖 引擎: {self.current_model}\n"
            f"📦 依赖: {'✅ Pillow正常' if PyImage else '❌ Pillow缺失'}\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"防漏机制已启动 | 最高优先级事件拦截中"
        )
