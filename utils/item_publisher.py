import asyncio
import base64
import io
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from PIL import Image
from loguru import logger

from utils.xianyu_utils import generate_sign, trans_cookies


class ItemPublisher:
    """闲鱼商品发布服务。"""

    APP_KEY = "34839810"
    BASE_REFERER = "https://www.goofish.com/"
    BASE_ORIGIN = "https://www.goofish.com"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    )
    ALLOWED_DELIVERY_CHOICES = {"包邮", "按距离计费", "一口价", "无需邮寄"}
    CATEGORY_PATH_ERROR_CODE = "FAIL_BIZ_CHANNEL_CAT_ID_PATH_QUERY_ERROR"

    def __init__(self, cookies_str: str, cookie_id: str = "", proxy_config: Optional[Dict[str, Any]] = None):
        cleaned_cookies = str(cookies_str or "").strip()
        if not cleaned_cookies:
            raise ValueError("Cookie 为空，无法发布商品")

        self.cookie_id = str(cookie_id or "unknown").strip() or "unknown"
        self.cookies = trans_cookies(cleaned_cookies)
        self.cookies_str = self._serialize_cookies(self.cookies)
        self.proxy_config = self._normalize_proxy_config(proxy_config)
        self._http_proxy_url: Optional[str] = None
        self._http_proxy_auth: Optional[aiohttp.BasicAuth] = None
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        await self.create_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close_session()

    async def create_session(self):
        if self.session:
            return

        connector: Optional[aiohttp.BaseConnector] = None
        proxy_url = self._get_proxy_url()
        proxy_type = self.proxy_config.get("proxy_type", "none")
        if proxy_url:
            proxy_host = self.proxy_config.get("proxy_host", "")
            proxy_port = self.proxy_config.get("proxy_port", 0)
            logger.info(f"【{self.cookie_id}】商品发布使用账号代理: {proxy_type}://{proxy_host}:{proxy_port}")

            if proxy_type == "socks5":
                try:
                    from aiohttp_socks import ProxyConnector, ProxyType
                except ImportError as exc:
                    raise RuntimeError("SOCKS5 代理需要安装 aiohttp-socks，请先执行 pip install aiohttp-socks") from exc

                connector = ProxyConnector(
                    proxy_type=ProxyType.SOCKS5,
                    host=proxy_host,
                    port=proxy_port,
                    username=self.proxy_config.get("proxy_user") or None,
                    password=self.proxy_config.get("proxy_pass") or None,
                    rdns=True,
                )
            elif proxy_type in {"http", "https"}:
                self._http_proxy_url = proxy_url
                proxy_user = self.proxy_config.get("proxy_user") or ""
                if proxy_user:
                    self._http_proxy_auth = aiohttp.BasicAuth(
                        proxy_user,
                        self.proxy_config.get("proxy_pass") or "",
                        encoding="utf-8",
                    )
        elif proxy_type != "none":
            raise RuntimeError(f"账号代理配置不完整，商品发布不会直连: type={proxy_type}")

        session_kwargs: Dict[str, Any] = {}
        if connector is not None:
            session_kwargs["connector"] = connector

        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=40),
            headers={
                "User-Agent": self.USER_AGENT,
                "Accept-Language": "en,zh-CN;q=0.9,zh;q=0.8,zh-TW;q=0.7,ja;q=0.6",
            },
            **session_kwargs,
        )

    async def close_session(self):
        if self.session:
            await self.session.close()
            self.session = None
        self._http_proxy_url = None
        self._http_proxy_auth = None

    @staticmethod
    def _normalize_proxy_config(proxy_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(proxy_config, dict):
            proxy_config = {}

        proxy_type = str(proxy_config.get("proxy_type") or "none").strip().lower()
        if proxy_type in {"", "none"}:
            proxy_type = "none"
        elif proxy_type not in {"http", "https", "socks5"}:
            raise ValueError(f"不支持的代理类型: {proxy_type}")

        try:
            proxy_port = int(proxy_config.get("proxy_port") or 0)
        except (TypeError, ValueError):
            proxy_port = 0

        return {
            "proxy_type": proxy_type,
            "proxy_host": str(proxy_config.get("proxy_host") or "").strip(),
            "proxy_port": proxy_port if proxy_port > 0 else 0,
            "proxy_user": str(proxy_config.get("proxy_user") or "").strip(),
            "proxy_pass": str(proxy_config.get("proxy_pass") or ""),
        }

    def _get_proxy_url(self) -> Optional[str]:
        proxy_type = self.proxy_config.get("proxy_type", "none")
        if proxy_type == "none":
            return None

        proxy_host = self.proxy_config.get("proxy_host", "")
        proxy_port = self.proxy_config.get("proxy_port", 0)
        if not proxy_host or not proxy_port:
            return None

        host_for_url = proxy_host
        if ":" in host_for_url and not host_for_url.startswith("["):
            host_for_url = f"[{host_for_url}]"

        return f"{proxy_type}://{host_for_url}:{proxy_port}"

    @staticmethod
    def is_success_response(payload: Dict[str, Any]) -> bool:
        ret_values = payload.get("ret") if isinstance(payload, dict) else None
        if not isinstance(ret_values, list):
            return False
        return any(str(item).startswith("SUCCESS::") for item in ret_values)

    @staticmethod
    def extract_error_message(payload: Dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            return "响应格式异常"

        ret_values = payload.get("ret")
        if isinstance(ret_values, list) and ret_values:
            return str(ret_values[0])

        error_fields = (
            payload.get("message"),
            payload.get("errorMsg"),
            payload.get("msg"),
        )
        for value in error_fields:
            if value:
                return str(value)

        return "请求失败"

    @classmethod
    def is_category_path_error(cls, payload: Dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        ret_values = payload.get("ret") or []
        if not isinstance(ret_values, list):
            ret_values = [ret_values]
        return any(cls.CATEGORY_PATH_ERROR_CODE in str(item) for item in ret_values)

    @classmethod
    def build_category_path_error_message(cls, payload: Dict[str, Any]) -> str:
        raw_message = cls.extract_error_message(payload)
        return (
            "闲鱼渠道类目路径查询失败，通常是自动识别出的商品类目无效或不完整导致。"
            "请尝试修改商品标题、补充更明确的类目提示、更换首图后重试。"
            f"原始错误: {raw_message}"
        )

    @staticmethod
    def _first_present_value(data: Dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = data.get(key) if isinstance(data, dict) else None
            if value not in (None, ""):
                return value
        return ""

    @classmethod
    def _normalize_category_result(cls, category_result: Dict[str, Any]) -> Dict[str, str]:
        return {
            "catId": str(cls._first_present_value(category_result, "catId", "cat_id") or "").strip(),
            "catName": str(
                cls._first_present_value(
                    category_result,
                    "catName",
                    "cat_name",
                    "channelCatName",
                    "channel_cat_name",
                    "channelCateName",
                ) or ""
            ).strip(),
            "channelCatId": str(
                cls._first_present_value(
                    category_result,
                    "channelCatId",
                    "channel_cat_id",
                    "channelCateId",
                ) or ""
            ).strip(),
            "tbCatId": str(cls._first_present_value(category_result, "tbCatId", "tb_cat_id") or "").strip(),
        }

    @staticmethod
    def _is_selected_category_value(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value == 1
        return str(value or "").strip().lower() in {"1", "true", "yes", "selected"}

    @classmethod
    def _extract_category_candidates(cls, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        card_list = data.get("cardList") if isinstance(data, dict) else []
        for card_index, card in enumerate(card_list or []):
            card_data = card.get("cardData") if isinstance(card, dict) else None
            if not isinstance(card_data, dict):
                continue

            property_name = str(card_data.get("propertyName") or "")
            looks_like_category_card = any(keyword in property_name for keyword in ("类目", "分类", "品类"))
            for value_index, value in enumerate(card_data.get("valuesList") or []):
                if not isinstance(value, dict):
                    continue

                merged_value = dict(value)
                transport_data = value.get("transportData")
                if isinstance(transport_data, dict):
                    for key, aliases in {
                        "catName": ("catName", "channelCatName", "channelCateName"),
                        "channelCatId": ("channelCatId", "channelCateId"),
                        "tbCatId": ("tbCatId",),
                    }.items():
                        if cls._first_present_value(merged_value, key):
                            continue
                        fallback = cls._first_present_value(transport_data, *aliases)
                        if fallback not in (None, ""):
                            merged_value[key] = fallback

                normalized = cls._normalize_category_result(merged_value)
                if not looks_like_category_card and not any(normalized.values()):
                    continue

                try:
                    score = float(value.get("score") or 0)
                except (TypeError, ValueError):
                    score = 0.0
                selected = cls._is_selected_category_value(value.get("isClicked")) or cls._is_selected_category_value(
                    value.get("isUserClick")
                )
                if isinstance(transport_data, dict):
                    selected = selected or cls._is_selected_category_value(transport_data.get("isUserClick"))

                candidates.append(
                    {
                        **normalized,
                        "selected": selected,
                        "score": score,
                        "card_index": card_index,
                        "value_index": value_index,
                    }
                )
        return candidates

    @staticmethod
    def _is_complete_category(category_result: Dict[str, Any]) -> bool:
        return all(str(category_result.get(field) or "").strip() for field in ("catId", "catName", "channelCatId", "tbCatId"))

    @staticmethod
    def _is_channel_category(category_result: Dict[str, Any]) -> bool:
        """新版频道类目只返回 channelCatId + catName。

        闲鱼近期把 AI 教学、AI 工具等类目迁移到了频道类目树，
        这类结果没有旧版的 catId/tbCatId。它们仍是平台明确返回的
        可发布类目，提交时应保留频道 ID，让平台完成最终校验。
        """
        return all(str(category_result.get(field) or "").strip() for field in ("catName", "channelCatId"))

    @classmethod
    def _validate_category_recommendation(cls, channel_res: Dict[str, Any]) -> Dict[str, str]:
        data = channel_res.get("data") if isinstance(channel_res, dict) else None
        data = data if isinstance(data, dict) else {}
        category_result = cls._normalize_category_result(
            data.get("categoryPredictResult", {})
        )
        if cls._is_complete_category(category_result):
            return category_result

        # 新版频道类目可能直接出现在主预测结果中，只有频道 ID 和名称。
        # 先保留平台的原始选择，不再因为缺少旧版 tbCatId 而在本地拦截。
        if cls._is_channel_category(category_result):
            logger.info(
                "闲鱼返回新版频道类目，按频道类目提交: "
                f"{category_result.get('catName')} ({category_result.get('channelCatId')})"
            )
            return category_result

        candidates = cls._extract_category_candidates(data)
        selected_candidates = [candidate for candidate in candidates if candidate.get("selected")]
        complete_selected = [candidate for candidate in selected_candidates if cls._is_complete_category(candidate)]
        channel_selected = [candidate for candidate in selected_candidates if cls._is_channel_category(candidate)]

        # categoryPredictResult 偶尔会为空，但闲鱼仍会在“分类”卡片中把实际选中的
        # 完整类目返回。只采用明确标记为选中的候选，避免擅自改到另一个类目。
        if complete_selected:
            selected = max(complete_selected, key=lambda item: float(item.get("score") or 0))
            resolved = {field: str(selected.get(field) or "").strip() for field in category_result}
            logger.warning(
                "闲鱼主类目预测为空或不完整，已采用平台明确选中的候选类目: "
                f"{resolved.get('catName')} ({resolved.get('channelCatId')})"
            )
            return resolved

        # AI 教学服务等新版类目通常只有 channelCatId/catName，且由平台在分类卡片
        # 中标记 isClicked。优先使用这个明确选项，不拼接其他候选的旧版 ID。
        if channel_selected:
            selected = max(channel_selected, key=lambda item: float(item.get('score') or 0))
            resolved = {field: str(selected.get(field) or "").strip() for field in category_result}
            logger.info(
                "闲鱼分类卡片已选新版频道类目，按频道类目提交: "
                f"{resolved.get('catName')} ({resolved.get('channelCatId')})"
            )
            return resolved

        # 主结果为部分字段时，只允许使用与这些字段完全一致的候选补齐，
        # 防止把相邻候选的 ID 拼成一个不存在的类目路径。
        present_prediction = {field: value for field, value in category_result.items() if value}
        if present_prediction:
            compatible_candidates = [
                candidate
                for candidate in candidates
                if cls._is_complete_category(candidate)
                and all(str(candidate.get(field) or "") == value for field, value in present_prediction.items())
            ]
            if len(compatible_candidates) == 1:
                compatible = compatible_candidates[0]
                return {field: str(compatible.get(field) or "").strip() for field in category_result}

        missing_fields = [field for field, value in category_result.items() if not value]
        candidate_summaries = []
        for candidate in candidates[:5]:
            name = candidate.get("catName") or "未命名类目"
            candidate_missing = [
                field for field in ("catId", "channelCatId", "tbCatId") if not candidate.get(field)
            ]
            suffix = "完整" if not candidate_missing else f"缺 {','.join(candidate_missing)}"
            selected_mark = "，平台已选" if candidate.get("selected") else ""
            candidate_summaries.append(f"{name}（{suffix}{selected_mark}）")

        candidate_message = ""
        if candidate_summaries:
            candidate_message = f" 平台候选: {'、'.join(candidate_summaries)}。"
        raise ValueError(
            "自动识别类目不完整，缺少字段: "
            f"{', '.join(missing_fields)}。{candidate_message}"
            "系统不会用其他类目伪装发布；请填写真实、具体的商品类型，或调整标题和首图后重试。"
        )

    @classmethod
    def _build_category_debug(cls, channel_res: Dict[str, Any], category_hint: Optional[str] = None) -> Dict[str, Any]:
        data = channel_res.get("data") if isinstance(channel_res, dict) else None
        data = data if isinstance(data, dict) else {}
        return {
            "category_hint": str(category_hint or "").strip(),
            "categoryPredictResult": data.get("categoryPredictResult") or {},
            "cardList": data.get("cardList") or [],
        }

    @staticmethod
    def _build_recommend_text(title: str, description: str, category_hint: Optional[str] = None) -> Tuple[str, str]:
        clean_title = str(title or "").strip()
        clean_description = str(description or clean_title or "").strip()
        clean_hint = str(category_hint or "").strip()
        if not clean_hint:
            return clean_title, clean_description or clean_title

        recommend_title = clean_title
        if clean_hint not in clean_title:
            recommend_title = f"{clean_hint} {clean_title}".strip()

        hint_line = f"类目提示：{clean_hint}"
        recommend_description = f"{hint_line}\n{clean_description}" if clean_description else hint_line
        return recommend_title[:120], recommend_description[:5000]

    @classmethod
    def extract_published_item_id(cls, payload: Dict[str, Any]) -> Optional[str]:
        data = payload.get("data") if isinstance(payload, dict) else None
        return cls._search_item_id(data)

    @classmethod
    def _search_item_id(cls, node: Any) -> Optional[str]:
        if isinstance(node, dict):
            for key in ("itemId", "item_id", "idleItemId", "idleId"):
                value = node.get(key)
                normalized = cls._normalize_candidate_id(value)
                if normalized:
                    return normalized

            for value in node.values():
                found = cls._search_item_id(value)
                if found:
                    return found

        if isinstance(node, list):
            for item in node:
                found = cls._search_item_id(item)
                if found:
                    return found

        return None

    @staticmethod
    def _normalize_candidate_id(value: Any) -> Optional[str]:
        text = str(value or "").strip()
        if text and text.isdigit() and len(text) >= 6:
            return text
        return None

    async def publish_item(
        self,
        *,
        title: str,
        description: str,
        images: List[Dict[str, Any]],
        current_price: Optional[float],
        original_price: Optional[float],
        delivery_choice: str,
        post_price: Optional[float],
        can_self_pickup: bool,
        category_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        if delivery_choice not in self.ALLOWED_DELIVERY_CHOICES:
            raise ValueError("不支持的运费方式")

        if delivery_choice == "一口价" and (post_price is None or post_price < 0):
            raise ValueError("运费方式为一口价时，邮费必须大于等于 0")

        uploaded_images = []
        for image in images:
            uploaded_images.append(await self.prepare_image_for_publish(image))

        publish_title = str(title or "").strip()
        publish_desc = str(description or title or "").strip()
        if not publish_title:
            raise ValueError("商品标题不能为空")
        if not publish_desc:
            raise ValueError("商品描述不能为空")

        cleaned_category_hint = str(category_hint or "").strip() or None
        attempt_hints = [cleaned_category_hint, None if cleaned_category_hint else None]
        channel_res: Dict[str, Any] = {}
        category_result: Optional[Dict[str, str]] = None
        category_errors: List[str] = []
        successful_recommendation_seen = False

        for attempt_index, attempt_hint in enumerate(attempt_hints):
            if attempt_index:
                # 类目推荐接口偶尔会返回 SUCCESS 但主结果为空。短暂等待后重试一次；
                # 用户填写的提示词可能不是闲鱼真实类目，因此第二次自动去掉提示词。
                await asyncio.sleep(0.45)
            channel_res = await self.get_public_channel(
                publish_title,
                publish_desc,
                uploaded_images,
                category_hint=attempt_hint,
            )
            if not self.is_success_response(channel_res):
                category_errors.append(f"获取发布类目失败: {self.extract_error_message(channel_res)}")
                continue

            successful_recommendation_seen = True
            try:
                category_result = self._validate_category_recommendation(channel_res)
                break
            except ValueError as exc:
                category_errors.append(str(exc))
                if attempt_index == 0:
                    logger.warning(
                        f"【{self.cookie_id}】闲鱼类目识别结果不完整，准备自动重试: {exc}"
                    )

        if category_result is None:
            last_error = category_errors[-1] if category_errors else "闲鱼未返回类目识别结果"
            retry_note = "已去除类目提示并自动重试一次" if cleaned_category_hint else "已自动重试一次"
            if successful_recommendation_seen:
                raise ValueError(f"{last_error}（{retry_note}）")
            raise RuntimeError(f"{last_error}（{retry_note}）")

        category_debug = self._build_category_debug(channel_res, category_hint=attempt_hint)
        category_debug["requested_category_hint"] = cleaned_category_hint or ""
        category_debug["recommendation_attempts"] = len(category_errors) + 1

        location = await self.get_default_location()

        publish_data = self._build_publish_payload(
            title=publish_title,
            description=publish_desc,
            uploaded_images=uploaded_images,
            channel_res=channel_res,
            location=location,
            current_price=current_price,
            original_price=original_price,
            delivery_choice=delivery_choice,
            post_price=post_price,
            can_self_pickup=can_self_pickup,
            category_result=category_result,
        )

        publish_res = await self._post_mtop(
            api_name="mtop.idle.pc.idleitem.publish",
            version="1.0",
            payload=publish_data,
            spm_cnt="a21ybx.publish.0.0",
            spm_pre="a21ybx.home.sidebar.1.46413da6EPl7v5",
        )

        publish_res["_uploaded_images"] = uploaded_images
        publish_res["_category_debug"] = category_debug
        return publish_res

    async def prepare_image_for_publish(self, image: Dict[str, Any]) -> Dict[str, Any]:
        """将前端/素材中的图片描述统一转换为发布接口可用的图片信息。"""
        if not isinstance(image, dict):
            raise ValueError("图片数据格式无效")

        image_url = str(image.get("url") or image.get("image_url") or image.get("src") or "").strip()
        if image_url:
            return {
                "url": image_url,
                "width": self._parse_dimension(image.get("width") or image.get("widthSize"), 800),
                "height": self._parse_dimension(image.get("height") or image.get("heightSize"), 800),
            }

        image_content = image.get("content") or image.get("data") or image.get("base64")
        if isinstance(image_content, str):
            image_content = self._decode_base64_image(image_content)
        if not isinstance(image_content, (bytes, bytearray)):
            raise ValueError("图片缺少可上传内容")

        return await self.upload_image(
            image_bytes=bytes(image_content),
            filename=image.get("filename") or image.get("name") or "item.jpg",
        )

    async def upload_image(self, *, image_bytes: bytes, filename: str) -> Dict[str, Any]:
        if not image_bytes:
            raise ValueError("图片内容为空")

        await self.create_session()

        normalized_bytes, width, height = self._normalize_image(image_bytes)
        safe_filename = self._normalize_filename(filename)

        form_data = aiohttp.FormData()
        form_data.add_field(
            "file",
            normalized_bytes,
            filename=safe_filename,
            content_type="image/jpeg",
        )

        headers = self._build_headers()
        headers.pop("content-type", None)
        headers.update({
            "accept": "application/json, text/javascript, */*; q=0.01",
            "x-requested-with": "XMLHttpRequest",
        })
        params = {
            "floderId": "0",
            "appkey": "fleamarket",
            "_input_charset": "utf-8",
        }

        async with self.session.post(
            "https://stream-upload.goofish.com/api/upload.api",
            params=params,
            data=form_data,
            headers=headers,
            allow_redirects=False,
            **self._build_request_kwargs(),
        ) as response:
            self._update_cookies_from_response(response)
            status = response.status
            location = str(response.headers.get("Location") or "")
            content_type = str(response.headers.get("Content-Type") or "").lower()
            response_text = await response.text()

        if status in {301, 302, 303, 307, 308}:
            if "login" in location.lower():
                raise ValueError(
                    "闲鱼图片上传登录态已失效（上传服务返回登录页）。"
                    "请到账号管理重新登录该闲鱼账号，获取最新 Cookie 后再发布；"
                    "浏览器里能查看闲鱼页面，不代表本系统保存的图片上传登录态仍有效。"
                )
            raise RuntimeError(f"闲鱼图片上传发生异常跳转（HTTP {status}），请稍后重试")

        if status in {401, 403}:
            raise ValueError(
                f"闲鱼图片上传未授权（HTTP {status}）。请到账号管理重新登录该闲鱼账号后重试。"
            )

        if status < 200 or status >= 300:
            raise RuntimeError(f"闲鱼图片上传请求失败（HTTP {status}）")

        response_prefix = response_text.lstrip().lower()
        if "text/html" in content_type or response_prefix.startswith("<!doctype html") or response_prefix.startswith("<html"):
            raise ValueError(
                "闲鱼图片上传登录态已失效（上传服务返回登录页面）。"
                "请到账号管理重新登录该闲鱼账号后重试。"
            )

        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"闲鱼图片上传返回了无法解析的数据（HTTP {status}，Content-Type: {content_type or '未知'}）"
            ) from exc

        image_object = payload.get("object") or payload.get("data") or payload.get("result") or {}
        image_url = image_object.get("url") or payload.get("url")
        if not image_url:
            raise RuntimeError(f"图片上传失败: {self.extract_error_message(payload)}")

        pixel_text = image_object.get("pix") or ""
        if isinstance(pixel_text, str) and "x" in pixel_text:
            try:
                width, height = [int(part) for part in pixel_text.lower().split("x", 1)]
            except (TypeError, ValueError):
                pass

        return {
            "url": image_url,
            "width": width,
            "height": height,
        }

    async def get_public_channel(
        self,
        title: str,
        description: str,
        images_info: List[Dict[str, Any]],
        category_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        recommend_title, recommend_description = self._build_recommend_text(title, description, category_hint)
        payload = {
            "title": recommend_title,
            "lockCpv": False,
            "multiSKU": False,
            "publishScene": "mainPublish",
            "scene": "newPublishChoice",
            "description": recommend_description,
            "imageInfos": [
                {
                    "extraInfo": {
                        "isH": "false",
                        "isT": "false",
                        "raw": "false",
                    },
                    "isQrCode": False,
                    "url": image["url"],
                    "heightSize": image["height"],
                    "widthSize": image["width"],
                    "major": True,
                    "type": 0,
                    "status": "done",
                }
                for image in images_info
            ],
            "uniqueCode": self._build_unique_code(),
        }

        return await self._post_mtop(
            api_name="mtop.taobao.idle.kgraph.property.recommend",
            version="2.0",
            payload=payload,
            spm_cnt="a21ybx.publish.0.0",
            spm_pre="a21ybx.item.sidebar.1.67321598K9Vgx8",
        )

    async def get_default_location(self) -> Dict[str, Any]:
        payload = {
            "longitude": 118.78248347393424,
            "latitude": 31.91629189813543,
        }
        result = await self._post_mtop(
            api_name="mtop.taobao.idle.local.poi.get",
            version="1.0",
            payload=payload,
            spm_cnt="a21ybx.publish.0.0",
            spm_pre="a21ybx.item.sidebar.1.38262218ame5nr",
            extra_headers={
                "eagleeye-userdata": "spm-cnt=a21ybx",
            },
        )

        if not self.is_success_response(result):
            raise RuntimeError(f"获取默认地址失败: {self.extract_error_message(result)}")

        address_list = (
            result.get("data", {}).get("commonAddresses")
            if isinstance(result.get("data"), dict)
            else None
        ) or []
        if not address_list:
            raise RuntimeError("未获取到账号默认地址")
        return address_list[0]

    def _build_publish_payload(
        self,
        *,
        title: str,
        description: str,
        uploaded_images: List[Dict[str, Any]],
        channel_res: Dict[str, Any],
        location: Dict[str, Any],
        current_price: Optional[float],
        original_price: Optional[float],
        delivery_choice: str,
        post_price: Optional[float],
        can_self_pickup: bool,
        category_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        category_result = category_result or self._normalize_category_result(
            channel_res.get("data", {}).get("categoryPredictResult", {})
        )
        card_list = channel_res.get("data", {}).get("cardList", []) or []

        payload = {
            "freebies": False,
            "itemTypeStr": "b",
            "quantity": "1",
            "simpleItem": "true",
            "imageInfoDOList": [
                {
                    "extraInfo": {
                        "isH": "false",
                        "isT": "false",
                        "raw": "false",
                    },
                    "isQrCode": False,
                    "url": image["url"],
                    "heightSize": image["height"],
                    "widthSize": image["width"],
                    "major": True,
                    "type": 0,
                    "status": "done",
                }
                for image in uploaded_images
            ],
            "itemTextDTO": {
                "desc": description,
                "title": title,
                "titleDescSeparate": description != title,
            },
            "itemLabelExtList": self._build_item_label_list(card_list, selected_category=category_result),
            "itemPriceDTO": {},
            "userRightsProtocols": [
                {
                    "enable": False,
                    "serviceCode": "SKILL_PLAY_NO_MIND",
                }
            ],
            "itemPostFeeDTO": {
                "canFreeShipping": False,
                "supportFreight": False,
                "onlyTakeSelf": False,
            },
            "itemAddrDTO": {
                "area": location.get("area", ""),
                "city": location.get("city", ""),
                "divisionId": location.get("divisionId", 0),
                "gps": f"{location.get('longitude')},{location.get('latitude')}",
                "poiId": location.get("poiId", ""),
                "poiName": location.get("poi", ""),
                "prov": location.get("prov", ""),
            },
            "defaultPrice": False,
            "itemCatDTO": {
                "catId": str(category_result.get("catId", "")),
                "catName": str(category_result.get("catName", "")),
                "channelCatId": str(category_result.get("channelCatId", "")),
                "tbCatId": str(category_result.get("tbCatId", "")),
            },
            "uniqueCode": self._build_unique_code(),
            "sourceId": "pcMainPublish",
            "bizcode": "pcMainPublish",
            "publishScene": "pcMainPublish",
        }

        self._apply_delivery_settings(
            payload=payload,
            delivery_choice=delivery_choice,
            post_price=post_price,
            can_self_pickup=can_self_pickup,
        )
        self._apply_price_settings(
            payload=payload,
            current_price=current_price,
            original_price=original_price,
        )

        return payload

    def _apply_delivery_settings(
        self,
        *,
        payload: Dict[str, Any],
        delivery_choice: str,
        post_price: Optional[float],
        can_self_pickup: bool,
    ):
        post_fee = payload["itemPostFeeDTO"]

        if delivery_choice == "包邮":
            post_fee["canFreeShipping"] = True
            post_fee["supportFreight"] = True
        elif delivery_choice == "按距离计费":
            post_fee["supportFreight"] = True
            post_fee["templateId"] = "-100"
        elif delivery_choice == "一口价":
            post_fee["supportFreight"] = True
            post_fee["postPriceInCent"] = str(int(round((post_price or 0) * 100)))
            post_fee["templateId"] = "0"
        elif delivery_choice == "无需邮寄":
            post_fee["templateId"] = "0"

        if can_self_pickup:
            post_fee["onlyTakeSelf"] = True
            payload["onlyTakeSelf"] = True

    @staticmethod
    def _apply_price_settings(
        *,
        payload: Dict[str, Any],
        current_price: Optional[float],
        original_price: Optional[float],
    ):
        price_dto = payload["itemPriceDTO"]
        has_price = False

        if current_price is not None and current_price > 0:
            price_dto["priceInCent"] = str(int(round(current_price * 100)))
            has_price = True

        if original_price is not None and original_price > 0:
            price_dto["origPriceInCent"] = str(int(round(original_price * 100)))
            has_price = True

        if not has_price:
            payload["defaultPrice"] = True

    @classmethod
    def _build_item_label_list(
        cls,
        card_list: List[Dict[str, Any]],
        selected_category: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        labels: List[Dict[str, Any]] = []
        for card in card_list:
            card_data = card.get("cardData") or {}
            values_list = card_data.get("valuesList") or []
            selected_value = None

            selected_channel_cat_id = str((selected_category or {}).get("channelCatId") or "").strip()
            if selected_channel_cat_id:
                selected_value = next(
                    (
                        value for value in values_list
                        if str(
                            cls._first_present_value(
                                value,
                                "channelCatId",
                                "channel_cat_id",
                                "channelCateId",
                            ) or ""
                        ).strip() == selected_channel_cat_id
                    ),
                    None,
                )

            if selected_value is None:
                for value in values_list:
                    if cls._is_selected_category_value(value.get("isClicked")):
                        selected_value = value
                        break

            if selected_value is None:
                property_name = str(card_data.get("propertyName") or "")
                looks_like_category_card = any(keyword in property_name for keyword in ("类目", "分类", "品类"))
                if looks_like_category_card:
                    selected_value = next(
                        (
                            value for value in values_list
                            if cls._first_present_value(value, "channelCatId", "channel_cat_id")
                            and cls._first_present_value(value, "catName", "cat_name", "channelCatName", "channel_cat_name")
                        ),
                        None,
                    )

            if not selected_value:
                continue

            channel_cat_id = cls._first_present_value(selected_value, "channelCatId", "channel_cat_id")
            cat_name = cls._first_present_value(selected_value, "catName", "cat_name", "channelCatName", "channel_cat_name")
            tb_cat_id = cls._first_present_value(selected_value, "tbCatId", "tb_cat_id")
            if not channel_cat_id or not cat_name:
                continue

            labels.append(
                {
                    "channelCateName": cat_name,
                    "valueId": None,
                    "channelCateId": channel_cat_id,
                    "valueName": None,
                    "tbCatId": tb_cat_id,
                    "subPropertyId": None,
                    "labelType": "common",
                    "subValueId": None,
                    "labelId": None,
                    "propertyName": card_data.get("propertyName"),
                    "isUserClick": "1",
                    "isUserCancel": None,
                    "from": "newPublishChoice",
                    "propertyId": card_data.get("propertyId"),
                    "labelFrom": "newPublish",
                    "text": cat_name,
                    "properties": (
                        f"{card_data.get('propertyId')}##{card_data.get('propertyName')}:"
                        f"{channel_cat_id}##{cat_name}"
                    ),
                }
            )
        return labels

    async def _post_mtop(
        self,
        *,
        api_name: str,
        version: str,
        payload: Dict[str, Any],
        spm_cnt: str,
        spm_pre: str,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        await self.create_session()

        data_val = json.dumps(payload, separators=(",", ":"))
        params = {
            "jsv": "2.7.2",
            "appKey": self.APP_KEY,
            "t": str(int(time.time() * 1000)),
            "sign": "",
            "v": version,
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": api_name,
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": spm_cnt,
            "spm_pre": spm_pre,
            "log_id": self._build_log_id(),
        }

        token = self._get_token()
        params["sign"] = generate_sign(params["t"], token, data_val)
        headers = self._build_headers(extra_headers=extra_headers)
        url = f"https://h5api.m.goofish.com/h5/{api_name}/{version}/"

        async with self.session.post(
            url,
            params=params,
            data={"data": data_val},
            headers=headers,
            **self._build_request_kwargs(),
        ) as response:
            self._update_cookies_from_response(response)
            response_text = await response.text()

        try:
            return json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{api_name} 返回非 JSON 响应: {response_text[:200]}") from exc

    def _build_request_kwargs(self) -> Dict[str, Any]:
        if not self._http_proxy_url:
            return {}

        request_kwargs: Dict[str, Any] = {"proxy": self._http_proxy_url}
        if self._http_proxy_auth:
            request_kwargs["proxy_auth"] = self._http_proxy_auth
        return request_kwargs

    def _build_headers(self, *, extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "accept": "application/json",
            "cache-control": "no-cache",
            "content-type": "application/x-www-form-urlencoded",
            "origin": self.BASE_ORIGIN,
            "pragma": "no-cache",
            "priority": "u=1, i",
            "referer": self.BASE_REFERER,
            "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": self.USER_AGENT,
            "cookie": self.cookies_str,
        }
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def _get_token(self) -> str:
        token_cookie = self.cookies.get("_m_h5_tk", "")
        token = str(token_cookie).split("_", 1)[0].strip()
        if not token:
            raise RuntimeError("Cookie 中缺少 _m_h5_tk，无法生成签名")
        return token

    def _update_cookies_from_response(self, response: aiohttp.ClientResponse) -> bool:
        updated = False
        for cookie_name, morsel in response.cookies.items():
            cookie_value = morsel.value
            if not cookie_value:
                continue
            if self.cookies.get(cookie_name) == cookie_value:
                continue
            self.cookies[cookie_name] = cookie_value
            updated = True

        if updated:
            self.cookies_str = self._serialize_cookies(self.cookies)
            logger.info(f"【{self.cookie_id}】发布服务已更新响应 Cookie")

        return updated

    @staticmethod
    def _serialize_cookies(cookies: Dict[str, str]) -> str:
        return "; ".join(f"{key}={value}" for key, value in cookies.items() if str(key).strip())

    @staticmethod
    def _decode_base64_image(value: str) -> bytes:
        text = str(value or "").strip()
        if not text:
            raise ValueError("图片内容为空")
        if "," in text and text.lower().startswith("data:"):
            text = text.split(",", 1)[1]
        try:
            return base64.b64decode(text, validate=False)
        except Exception as exc:
            raise ValueError("图片 Base64 内容无效") from exc

    @staticmethod
    def _parse_dimension(value: Any, default: int = 800) -> int:
        try:
            dimension = int(float(value))
            return dimension if dimension > 0 else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_filename(filename: str) -> str:
        stem = os.path.splitext(os.path.basename(str(filename or "item")))[0] or "item"
        safe_stem = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in stem)
        return f"{safe_stem}.jpg"

    @staticmethod
    def _build_unique_code() -> str:
        return str(int(time.time() * 1000000))

    @staticmethod
    def _build_log_id() -> str:
        return f"publish{int(time.time() * 1000)}"

    @staticmethod
    def _normalize_image(image_bytes: bytes) -> Tuple[bytes, int, int]:
        max_dimension = 2048
        image = Image.open(io.BytesIO(image_bytes))

        if image.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", image.size, (255, 255, 255))
            if image.mode == "P":
                image = image.convert("RGBA")
            background.paste(image, mask=image.split()[-1] if image.mode in ("RGBA", "LA") else None)
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")

        width, height = image.size
        if width > max_dimension or height > max_dimension:
            ratio = min(max_dimension / width, max_dimension / height)
            width = max(1, int(width * ratio))
            height = max(1, int(height * ratio))
            image = image.resize((width, height), Image.Resampling.LANCZOS)

        output = io.BytesIO()
        image.save(output, format="JPEG", quality=88, optimize=True)
        return output.getvalue(), width, height
