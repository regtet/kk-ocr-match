# -*- coding: utf-8 -*-
"""
Umi-OCR 智能重命名助手
功能：选择A组和B组文件夹，自动OCR识别并匹配，将B组图片重命名为A组对应的图片名称

系统架构：
- 表现层：PySide6 窗口（现代简约风格）
- 业务逻辑层：Python 脚本（多线程 + 信号通信）
- 核心服务层：PaddleOCR-json.exe
"""

import os
import sys
import time
import tempfile
import hashlib
import copy
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from PIL import Image
from io import BytesIO


def get_base_dir() -> str:
    """
    获取程序运行的基础目录，兼容开发环境与 PyInstaller 打包后的 onefile 模式。
    
    - 开发环境：返回当前脚本所在目录
    - 打包后：返回可执行文件 exe 所在目录（用于查找旁边的 PaddleOCR-json_v1.4.1 文件夹）
    """
    # PyInstaller 冻结后的程序会设置 sys.frozen = True
    if getattr(sys, "frozen", False):
        # sys.executable 即打包后的 exe 路径
        return os.path.dirname(os.path.abspath(sys.executable))
    # 正常的 Python 运行环境
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(relative_path: str) -> str:
    """
    将相对路径转换为基于程序根目录的绝对路径。
    
    这里统一从「脚本 / exe 所在目录」开始，并返回绝对路径，
    方便采用「1 个 EXE + 1 个 OCR 文件夹」的半独立打包方案，
    同时避免多线程 / 子进程切换工作目录导致的相对路径问题。
    """
    return os.path.abspath(os.path.join(get_base_dir(), relative_path))

# PySide6 UI
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QTextEdit, QFileDialog, QProgressBar,
    QTableWidget, QTableWidgetItem, QMessageBox, QGroupBox, QSlider,
    QScrollArea, QListWidget, QListWidgetItem, QHeaderView, QFrame,
    QLineEdit, QGraphicsDropShadowEffect, QCheckBox, QButtonGroup, QSizePolicy
)
from PySide6.QtCore import Qt, Signal, QThread, QSize, QTimer, QPropertyAnimation, QEasingCurve, QPoint, QRect
from PySide6.QtGui import QPixmap, QIcon, QColor, QFont, QPainter, QPen, QBrush, QDragEnterEvent, QDropEvent, QImageReader

# OCR API
import subprocess
import json

# 模糊匹配
try:
    from fuzzywuzzy import fuzz
    FUZZYWUZZY_AVAILABLE = True
except ImportError:
    FUZZYWUZZY_AVAILABLE = False
    print("警告：未安装 fuzzywuzzy，将使用 difflib 作为备选")


# AVIF/HEIC 转换缓存：同一原图（按 path+mtime）仅转换一次，供 OCR 与缩略图复用
_CONVERTED_IMAGE_CACHE: Dict[str, Dict[str, object]] = {}


def get_cached_image_path(img_path: str) -> str:
    """获取可直接读取的图片路径；对 AVIF/HEIC 做一次性缓存转换后复用。"""
    abs_img_path = os.path.abspath(img_path)
    ext = Path(abs_img_path).suffix.lower()
    unsupported_formats = {".avif", ".heic", ".heif"}

    if ext not in unsupported_formats:
        return abs_img_path
    if not os.path.exists(abs_img_path):
        return abs_img_path

    try:
        mtime = os.path.getmtime(abs_img_path)
    except Exception:
        return abs_img_path

    cache_item = _CONVERTED_IMAGE_CACHE.get(abs_img_path)
    if cache_item:
        cached_path = str(cache_item.get("cached_path", ""))
        cached_mtime = cache_item.get("mtime")
        if cached_mtime == mtime and cached_path and os.path.exists(cached_path):
            return cached_path

    cache_dir = os.path.join(tempfile.gettempdir(), "ocr_rename_cache")
    os.makedirs(cache_dir, exist_ok=True)
    file_hash = hashlib.md5(f"{abs_img_path}|{mtime}".encode("utf-8")).hexdigest()
    jpg_path = os.path.join(cache_dir, f"{file_hash}.jpg")
    webp_path = os.path.join(cache_dir, f"{file_hash}.webp")

    try:
        with Image.open(abs_img_path) as img:
            if img.mode in ("RGBA", "LA", "P"):
                rgb_img = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                rgb_img.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                img = rgb_img
            elif img.mode != "RGB":
                img = img.convert("RGB")

            try:
                img.save(jpg_path, "JPEG", quality=90, optimize=True)
                cached_path = jpg_path
            except Exception:
                img.save(webp_path, "WEBP", quality=90, method=4)
                cached_path = webp_path

        old_cached = ""
        if cache_item:
            old_cached = str(cache_item.get("cached_path", ""))
        if old_cached and old_cached != cached_path and os.path.exists(old_cached):
            try:
                os.remove(old_cached)
            except Exception:
                pass

        _CONVERTED_IMAGE_CACHE[abs_img_path] = {"mtime": mtime, "cached_path": cached_path}
        return cached_path
    except Exception as e:
        print(f"[格式转换失败] {os.path.basename(img_path)}: {e}")
        return abs_img_path


def clear_image_conversion_cache():
    """清理运行期生成的转换缓存文件。"""
    for item in list(_CONVERTED_IMAGE_CACHE.values()):
        cached_path = str(item.get("cached_path", "")) if item else ""
        if cached_path and os.path.exists(cached_path):
            try:
                os.remove(cached_path)
            except Exception:
                pass
    _CONVERTED_IMAGE_CACHE.clear()


class OCRController:
    """直接控制 OCR 引擎"""
    
    def __init__(self, exe_path):
        self.exe_path = os.path.abspath(exe_path)
        self.proc = None
        self.exe_dir = os.path.dirname(self.exe_path)
    
    def start(self):
        """启动 OCR 引擎"""
        if not os.path.exists(self.exe_path):
            raise FileNotFoundError(f"OCR引擎不存在: {self.exe_path}")
        
        models_dir = os.path.join(self.exe_dir, "models")
        if not os.path.exists(models_dir):
            raise FileNotFoundError(f"模型文件夹不存在: {models_dir}")
        
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags = (
                subprocess.CREATE_NEW_CONSOLE | subprocess.STARTF_USESHOWWINDOW
            )
            startupinfo.wShowWindow = subprocess.SW_HIDE
        
        try:
            self.proc = subprocess.Popen(
                self.exe_path,
                cwd=self.exe_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                startupinfo=startupinfo,
            )
        except Exception as e:
            raise Exception(f"无法启动OCR引擎进程: {e}")
        
        print("[OCR初始化] 等待引擎初始化...")
        while True:
            if self.proc.poll() is not None:
                raise Exception("OCR引擎初始化失败：子进程已退出")
            try:
                initStr = self.proc.stdout.readline().decode("utf-8", errors="ignore")
                if "OCR init completed." in initStr or "初始化完成" in initStr:
                    print("[OCR初始化] 引擎初始化成功！")
                    break
            except Exception as e:
                raise Exception(f"OCR引擎初始化失败：{e}")
        
        print("[OCR初始化] 引擎就绪")
    
    def convert_image_if_needed(self, img_path):
        """返回 OCR 可直接读取的图片路径（含 AVIF/HEIC 缓存转换）。"""
        return get_cached_image_path(img_path)
    
    def get_text(self, img_path):
        """识别图片并提取文本"""
        if not self.proc or self.proc.poll() is not None:
            return ""
        
        abs_img_path = os.path.abspath(img_path)
        if not os.path.exists(abs_img_path):
            return ""
        
        actual_img_path = self.convert_image_if_needed(abs_img_path)
        
        try:
            writeDict = {"image_path": actual_img_path}
            writeStr = json.dumps(writeDict, ensure_ascii=True) + "\n"
            
            self.proc.stdin.write(writeStr.encode("utf-8"))
            self.proc.stdin.flush()
            
            getStr = self.proc.stdout.readline().decode("utf-8", errors="ignore")
            
            if not getStr:
                return ""
            
            try:
                data = json.loads(getStr)
                code = data.get("code")
                
                if code == 100:
                    texts = []
                    for item in data.get("data", []):
                        if isinstance(item, dict) and "text" in item:
                            texts.append(item["text"])
                    return "\n".join(texts)
                elif code == 101:
                    return ""
                else:
                    return ""
            except json.JSONDecodeError:
                return ""
                
        except Exception as e:
            print(f"[OCR错误] 识别异常 {os.path.basename(img_path)}: {e}")
            return ""
        finally:
            # 转换缓存由全局缓存管理，避免每次 OCR 反复转码
            pass
    
    def stop(self):
        """停止 OCR 引擎"""
        if self.proc:
            try:
                self.proc.kill()
            except:
                pass
            self.proc = None


class ImageSizeWorker(QThread):
    """异步读取图片尺寸的工作线程"""
    size_ready = Signal(str, int, int)  # img_path, width, height
    finished_batch = Signal()

    def __init__(self, image_paths: List[str]):
        super().__init__()
        self.image_paths = image_paths

    def run(self):
        for img_path in self.image_paths:
            if self.isInterruptionRequested():
                break
            try:
                if os.path.exists(img_path):
                    with Image.open(img_path) as img:
                        w, h = img.size
                        self.size_ready.emit(img_path, w, h)
            except Exception:
                pass
        self.finished_batch.emit()


class OCRWorker(QThread):
    """OCR识别工作线程（支持实时更新）"""
    progress = Signal(str, str, str)  # 图片路径, OCR文本, 状态消息
    finished = Signal()
    
    def __init__(self, ocr_controller, image_paths: List[str], group_name: str):
        super().__init__()
        self.ocr_controller = ocr_controller
        self.image_paths = image_paths
        self.group_name = group_name
        self.results = {}
    
    def run(self):
        """执行OCR识别"""
        total = len(self.image_paths)
        for i, img_path in enumerate(self.image_paths):
            # 如果外部请求中断（例如窗口关闭时），提前安全退出，避免 QThread 还在运行就被销毁
            if self.isInterruptionRequested():
                break
            try:
                self.progress.emit(
                    img_path,
                    "",  # 先发送空文本，表示正在识别
                    f"正在识别{self.group_name}: {i+1}/{total} - {os.path.basename(img_path)}"
                )
                
                text = self.ocr_controller.get_text(img_path)
                self.results[img_path] = text
                
                self.progress.emit(
                    img_path,
                    text,  # 发送识别结果
                    f"✓ {self.group_name}: {i+1}/{total} - {os.path.basename(img_path)} 识别完成"
                )
                
                time.sleep(0.1)
            except Exception as e:
                print(f"[错误] 识别异常 {os.path.basename(img_path)}: {e}")
                self.results[img_path] = ""
                self.progress.emit(
                    img_path,
                    "",
                    f"✗ {self.group_name}: {i+1}/{total} - {os.path.basename(img_path)} 识别失败: {e}"
                )
        
        self.finished.emit()


class ImageCard(QFrame):
    """图片卡片组件 - 图片在上，名称和文字在下"""
    clicked = Signal(str)          # 点击信号：卡片被点击
    double_clicked = Signal(str)   # 双击信号：卡片被双击
    delete_clicked = Signal(str)   # 删除信号：右上角删除按钮
    _PIXMAP_CACHE: Dict[str, QPixmap] = {}
    _PIXMAP_CACHE_ORDER: List[str] = []
    _PIXMAP_CACHE_MAX: int = 220
    
    def __init__(self, img_path: str, filename: str, ocr_text: str = "", parent=None):
        super().__init__(parent)
        self.img_path = img_path
        self.full_text = ocr_text or ""
        # 控制卡片宽度适中，避免页面被横向撑得过宽
        self.setFixedWidth(320)
        self.setStyleSheet("""
            QFrame {
                background-color: white;
                border: 1px solid #DDDDDD;
                border-radius: 8px;
                padding: 8px;
            }
            QFrame:hover {
                border: 1px solid #0078D4;
                background-color: #f7f9fb;
            }
        """)
        
        # 整体采用更紧凑的纵向布局，让图片占用绝大部分空间
        layout = QVBoxLayout(self)
        layout.setSpacing(3)
        layout.setContentsMargins(6, 6, 6, 6)

        # 顶部工具栏：右上角删除按钮
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 0, 0)
        top_bar.setSpacing(4)

        top_bar.addStretch()
        self.delete_btn = QPushButton("✕")
        self.delete_btn.setFixedSize(20, 20)
        self.delete_btn.setCursor(Qt.PointingHandCursor)
        self.delete_btn.setToolTip("删除此图片")
        self.delete_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(0,0,0,0);
                border: none;
                color: #A19F9D;
                font-size: 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                color: #D83B01;
            }
            QPushButton:pressed {
                color: #A80000;
            }
        """)
        self.delete_btn.clicked.connect(lambda: self.delete_clicked.emit(self.img_path))
        top_bar.addWidget(self.delete_btn)
        layout.addLayout(top_bar)
        
        # 图片显示（更大更清晰，统一比例）
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setStyleSheet("""
            QLabel {
                background-color: #f8f9fa;
                border: none;
                border-radius: 4px;
                /* 固定高度，让栅格更整齐 */
                min-height: 220px;
                max-height: 220px;
            }
        """)
        # 不直接拉伸内容，由代码中根据比例预缩放 QPixmap
        self.image_label.setScaledContents(False)
        # 延迟加载缩略图，避免批量创建卡片时主线程阻塞导致卡顿
        QTimer.singleShot(0, lambda p=img_path: self.load_image(p))
        layout.addWidget(self.image_label)

        # 匹配状态角标：真正悬浮在卡片上方（不参与任何布局）
        # 注意：父对象设为 self，而不是 image_label，避免被布局挤压拉伸
        self.status_label = QLabel("", self)
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setFixedSize(22, 22)
        self.status_label.setMinimumSize(22, 22)
        self.status_label.setMaximumSize(22, 22)
        self.status_label.setStyleSheet("""
            QLabel {
                background-color: rgba(0, 0, 0, 120);
                color: #FFFFFF;
                font-size: 12px;
                border-radius: 11px;
                min-width: 22px;
                max-width: 22px;
                min-height: 22px;
                max-height: 22px;
            }
        """)
        self.status_label.hide()

        # 尺寸与文件名行（同一行：左尺寸，右名称）
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(4)

        self.size_label = QLabel("")
        self.size_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.size_label.setStyleSheet("""
            QLabel {
                color: #605E5C;
                font-size: 10px;
                padding: 0px;
                border: none;
                background-color: transparent;
            }
        """)
        name_row.addWidget(self.size_label)

        name_row.addStretch()

        self.name_label = QLabel(filename)
        self.name_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.name_label.setWordWrap(False)
        name_font = QFont()
        name_font.setBold(True)
        name_font.setPointSize(10)
        self.name_label.setFont(name_font)
        self.name_label.setStyleSheet("""
            QLabel {
                color: #323130;
                padding: 0px;
                border: none;
                background-color: transparent;
            }
        """)
        name_row.addWidget(self.name_label, 1)
        layout.addLayout(name_row)
        
        # OCR文字：常驻显示在文件名下方，展示少量关键词摘要
        self.text_label = QLabel("")
        self.text_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.text_label.setWordWrap(True)
        self.text_label.setStyleSheet("""
            QLabel {
                color: #555;
                font-size: 10px;
                padding: 0px;
                border: none;
                background-color: transparent;
            }
        """)
        self.text_label.setMaximumHeight(32)
        layout.addWidget(self.text_label)

        # 初始化时如果已有 OCR 文本，填充摘要
        if ocr_text:
            self.update_text(ocr_text)
        
        # 设置鼠标事件
        self.setCursor(Qt.PointingHandCursor)
    
    # 缩略图最大边长，大图先缩小再显示，减轻卡顿
    _THUMB_MAX = 600

    @classmethod
    def _pixmap_cache_get(cls, key: str) -> Optional[QPixmap]:
        pix = cls._PIXMAP_CACHE.get(key)
        if pix is None:
            return None
        # 轻量 LRU：命中后移动到末尾
        try:
            cls._PIXMAP_CACHE_ORDER.remove(key)
        except ValueError:
            pass
        cls._PIXMAP_CACHE_ORDER.append(key)
        return pix

    @classmethod
    def _pixmap_cache_put(cls, key: str, pixmap: QPixmap):
        if pixmap.isNull():
            return
        if key in cls._PIXMAP_CACHE:
            cls._PIXMAP_CACHE[key] = pixmap
            try:
                cls._PIXMAP_CACHE_ORDER.remove(key)
            except ValueError:
                pass
            cls._PIXMAP_CACHE_ORDER.append(key)
            return
        cls._PIXMAP_CACHE[key] = pixmap
        cls._PIXMAP_CACHE_ORDER.append(key)
        while len(cls._PIXMAP_CACHE_ORDER) > cls._PIXMAP_CACHE_MAX:
            evict_key = cls._PIXMAP_CACHE_ORDER.pop(0)
            cls._PIXMAP_CACHE.pop(evict_key, None)

    def load_image(self, img_path: str):
        """加载图片（支持多格式；大图自动缩略以减轻卡顿）"""
        try:
            if not os.path.exists(img_path):
                self.image_label.setText(f"文件不存在\n{os.path.basename(img_path)}")
                print(f"[图片加载] 文件不存在: {img_path}")
                return

            # 缩略图显示区域固定，缓存键包含 path+mtime+目标尺寸，避免重复解码与缩放
            target_width = max(160, self.width() - 40)
            target_height = 220
            src_mtime = os.path.getmtime(img_path)
            cache_key = f"{os.path.abspath(img_path)}|{src_mtime}|{target_width}x{target_height}"
            cached_scaled = self._pixmap_cache_get(cache_key)
            if cached_scaled is not None and not cached_scaled.isNull():
                self.image_label.setPixmap(cached_scaled)
                self.image_label.setFixedHeight(target_height)
                self.image_label.setToolTip(f"双击查看大图\n{img_path}")
                return

            source_path = get_cached_image_path(img_path)
            pixmap = None
            reader = QImageReader(source_path)
            if reader.canRead():
                sz = reader.size()
                if sz.isValid() and (sz.width() > self._THUMB_MAX or sz.height() > self._THUMB_MAX):
                    scale = min(self._THUMB_MAX / sz.width(), self._THUMB_MAX / sz.height())
                    new_w = max(1, int(sz.width() * scale))
                    new_h = max(1, int(sz.height() * scale))
                    reader.setScaledSize(QSize(new_w, new_h))
                    img = reader.read()
                    if not img.isNull():
                        pixmap = QPixmap.fromImage(img)
            if pixmap is None or pixmap.isNull():
                pixmap = QPixmap(source_path)

            # 如果QPixmap加载失败，尝试用PIL转换格式
            if pixmap.isNull():
                # QPixmap 失败时尝试使用 PIL 兜底，不再频繁打印日志
                try:
                    # 使用PIL打开图片（支持更多格式，如AVIF）
                    with Image.open(source_path) as pil_img:
                        # 转换为RGB模式（QPixmap需要）
                        if pil_img.mode in ('RGBA', 'LA', 'P'):
                            rgb_img = Image.new('RGB', pil_img.size, (255, 255, 255))
                            if pil_img.mode == 'P':
                                pil_img = pil_img.convert('RGBA')
                            rgb_img.paste(pil_img, mask=pil_img.split()[-1] if pil_img.mode in ('RGBA', 'LA') else None)
                            pil_img = rgb_img
                        elif pil_img.mode != 'RGB':
                            pil_img = pil_img.convert('RGB')
                        
                        # 转换为字节数据
                        img_bytes = BytesIO()
                        pil_img.save(img_bytes, format='PNG')
                        img_bytes.seek(0)
                        
                        # 从字节数据创建QPixmap
                        pixmap = QPixmap()
                        pixmap.loadFromData(img_bytes.read(), 'PNG')
                        
                        if pixmap.isNull():
                            raise Exception("PIL转换后QPixmap仍加载失败")
                        
                        # PIL 转换成功，无需打印日志，避免大量拖拽时刷屏
                except Exception as pil_error:
                    print(f"[图片加载] PIL转换失败 {img_path}: {pil_error}")
                    self.image_label.setText(f"加载失败\n{os.path.basename(img_path)}\n{str(pil_error)[:30]}")
                    return
            
            # 按固定宽高比缩放（不裁剪），保证卡片高度统一且完整显示
            if not pixmap.isNull():
                scaled_pixmap = pixmap.scaled(
                    target_width,
                    target_height,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
                self._pixmap_cache_put(cache_key, scaled_pixmap)
                self.image_label.setPixmap(scaled_pixmap)
                # 固定高度，保持所有卡片排版统一
                self.image_label.setFixedHeight(target_height)
                self.image_label.setToolTip(f"双击查看大图\n{img_path}")
                # 成功加载时不再逐张打印，避免大量 I/O 造成拖拽卡顿
            else:
                self.image_label.setText(f"无法加载\n{os.path.basename(img_path)}")
                print(f"[图片加载] 最终加载失败: {img_path}")
                
        except Exception as e:
            error_msg = str(e)
            print(f"[图片加载] 异常 {img_path}: {error_msg}")
            import traceback
            traceback.print_exc()
            self.image_label.setText(f"加载异常\n{os.path.basename(img_path)}\n{error_msg[:30]}")
    
    def update_text(self, text: str):
        """更新OCR文字"""
        if text:
            self.full_text = text
            # 仅保留若干关键词作为摘要，在 hover 时才显示
            words = [w for w in text.replace("\n", " ").split(" ") if w.strip()]
            summary = " ".join(words[:3])
            if len(words) > 3:
                summary += " ..."
            self.text_label.setText(summary)
            self.text_label.setToolTip(text)  # 完整文本在tooltip中
        else:
            self.full_text = ""
            self.text_label.setText("")

    def update_size(self, width: int, height: int):
        """更新尺寸标签"""
        if width and height:
            self.size_label.setText(f"{width}x{height}")
            self.size_label.show()
        else:
            self.size_label.clear()
            self.size_label.hide()
    
    def mousePressEvent(self, event):
        """鼠标点击事件"""
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.img_path)
        super().mousePressEvent(event)
    
    def mouseDoubleClickEvent(self, event):
        """鼠标双击事件"""
        if event.button() == Qt.LeftButton:
            self.double_clicked.emit(self.img_path)
        super().mouseDoubleClickEvent(event)
    
    def set_selected(self, selected: bool):
        """设置选中状态"""
        if selected:
            self.setStyleSheet("""
                QFrame {
                    background-color: #e7f3ff;
                    border: 1px solid #0078D4;
                    border-radius: 8px;
                    padding: 8px;
                }
            """)
        else:
            self.setStyleSheet("""
                QFrame {
                    background-color: white;
                    border: 1px solid #DDDDDD;
                    border-radius: 8px;
                    padding: 8px;
                }
                QFrame:hover {
                    border: 1px solid #0078D4;
                    background-color: #f7f9fb;
                }
            """)

    def set_status(self, status: str):
        """设置卡片状态图标与背景色
        status: 'matched' | 'candidate' | 'pending'
        """
        if status == "matched":
            self.status_label.setText("✅")
            # 深绿色状态更醒目
            self.status_label.setFixedSize(22, 22)
            self.status_label.setMinimumSize(22, 22)
            self.status_label.setMaximumSize(22, 22)
            self.status_label.setStyleSheet("""
                QLabel {
                    background-color: #107C10;
                    color: #FFFFFF;
                    font-size: 12px;
                    border-radius: 11px;
                }
            """)
            self.setStyleSheet("""
                QFrame {
                    background-color: #e1f7e1;
                    border: 1px solid #107C10;
                    border-radius: 8px;
                    padding: 8px;
                }
                QFrame:hover {
                    border: 1px solid #107C10;
                    background-color: #d4f3d4;
                }
            """)
        elif status == "candidate":
            self.status_label.setText("❓")
            # 深黄色状态更醒目
            self.status_label.setFixedSize(22, 22)
            self.status_label.setMinimumSize(22, 22)
            self.status_label.setMaximumSize(22, 22)
            self.status_label.setStyleSheet("""
                QLabel {
                    background-color: #C8A600;
                    color: #FFFFFF;
                    font-size: 12px;
                    border-radius: 11px;
                }
            """)
            self.setStyleSheet("""
                QFrame {
                    background-color: #fff6d1;
                    border: 1px solid #C8A600;
                    border-radius: 8px;
                    padding: 8px;
                }
                QFrame:hover {
                    border: 1px solid #C8A600;
                    background-color: #ffefb3;
                }
            """)
        elif status == "pending":
            self.status_label.setText("⏳")
            # 深灰色状态更醒目
            self.status_label.setFixedSize(22, 22)
            self.status_label.setMinimumSize(22, 22)
            self.status_label.setMaximumSize(22, 22)
            self.status_label.setStyleSheet("""
                QLabel {
                    background-color: #605E5C;
                    color: #FFFFFF;
                    font-size: 12px;
                    border-radius: 11px;
                }
            """)
            # 恢复为默认未选中样式
            self.set_selected(False)
        else:
            # 未知状态，不显示图标
            self.status_label.clear()
            self.set_selected(False)

        # 更新悬浮状态角标的位置（统一放在图片左上角，避免尺寸异常）
        if status in ("matched", "candidate", "pending"):
            # 基于图片区域的位置进行绝对定位，父对象为卡片本身
            margin = 8
            img_geo = self.image_label.geometry()
            x = img_geo.left() + margin
            y = img_geo.top() + margin
            self.status_label.setGeometry(x, y, 22, 22)
            self.status_label.raise_()
            self.status_label.show()
        else:
            self.status_label.hide()

    def resizeEvent(self, event):
        """重载卡片尺寸变化，保持悬浮状态角标位置正确"""
        super().resizeEvent(event)
        if self.status_label.isVisible():
            margin = 8
            img_geo = self.image_label.geometry()
            x = img_geo.left() + margin
            y = img_geo.top() + margin
            self.status_label.setGeometry(x, y, 22, 22)
            self.status_label.raise_()


class OCRImageMatcher(QMainWindow):
    """Umi-OCR 智能重命名助手主窗口"""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Umi-OCR 智能重命名助手")
        self.setGeometry(100, 100, 1600, 1000)
        
        # 数据存储
        self.group_a_folder: Optional[str] = None  # A组文件夹路径
        self.group_b_folder: Optional[str] = None  # B组文件夹路径
        self.group_a_images: List[str] = []
        self.group_b_images: List[str] = []
        self.group_a_texts: Dict[str, str] = {}
        self.group_b_texts: Dict[str, str] = {}
        self.group_a_info: Dict[str, dict] = {}
        self.group_b_info: Dict[str, dict] = {}
        self.matches: List[Tuple[str, str, float]] = []
        self.threshold = 0.70  # 默认70%，过高易导致无匹配

        # A/B 过滤模式：all | unmatched | matched
        self.a_filter_mode: str = "all"
        self.b_filter_mode: str = "all"
        
        # 卡片组件字典 {图片路径: 卡片Widget}
        self.a_cards: Dict[str, ImageCard] = {}
        self.b_cards: Dict[str, ImageCard] = {}
        self.selected_a_card: Optional[ImageCard] = None
        self.selected_b_card: Optional[ImageCard] = None
        
        # OCR结果缓存 {文件夹路径: {图片路径: OCR文本}}
        self.ocr_cache: Dict[str, Dict[str, str]] = {}

        # 当前 A 组焦点及对应的 B 组推荐列表（path -> rank）
        self.current_a_focus: Optional[str] = None
        self.b_suggestions: Dict[str, int] = {}
        # 防抖：点击 A 卡后延迟刷新 B 表
        self._pending_update_b_timer: Optional[QTimer] = None
        self._threshold_timer: Optional[QTimer] = None
        self._resize_debounce_timer: Optional[QTimer] = None

        # 多文件夹批处理状态（单A -> 多B）
        self.batch_mode_enabled: bool = False
        self.batch_tasks: List[Dict[str, object]] = []
        self.batch_current_index: int = -1
        self.batch_selected_index: int = -1
        self.batch_results: Dict[str, Dict[str, object]] = {}

        # OCR引擎
        self.ocr_controller = None
        self.exe_path = self.find_paddleocr_exe()
        
        if self.exe_path:
            try:
                self.ocr_controller = OCRController(self.exe_path)
                self.ocr_controller.start()
                print(f"[初始化] OCR引擎初始化成功！")
            except Exception as e:
                error_msg = f"OCR引擎初始化失败：\n\n{str(e)}"
                QMessageBox.critical(self, "错误", error_msg)
                self.ocr_controller = None
        else:
            QMessageBox.critical(
                self, "错误",
                "未找到 PaddleOCR-json.exe！\n\n"
                "请确保 PaddleOCR-json.exe 位于：\n"
                "PaddleOCR-json_v1.4.1/PaddleOCR-json.exe"
            )
        
        # 工作线程
        self.worker_a: Optional[OCRWorker] = None
        self.worker_b: Optional[OCRWorker] = None
        self.size_worker: Optional[ImageSizeWorker] = None

        self.init_ui()
    
    def find_paddleocr_exe(self):
        """查找 PaddleOCR-json.exe 的位置（兼容开发环境与打包后的 EXE）"""
        base_dir = get_base_dir()
        possible_paths = [
            # 推荐结构：exe / main.py 同级的 PaddleOCR-json_v1.4.1 目录
            resource_path(os.path.join("PaddleOCR-json_v1.4.1", "PaddleOCR-json.exe")),
            # 兜底：直接放在根目录下
            resource_path("PaddleOCR-json.exe"),
        ]
        
        for path in possible_paths:
            abs_path = os.path.abspath(path)
            if os.path.exists(abs_path):
                exe_dir = os.path.dirname(abs_path)
                models_dir = os.path.join(exe_dir, "models")
                if os.path.exists(models_dir):
                    return abs_path
        
        return None
    
    def init_ui(self):
        """初始化用户界面（现代简约风格 - 详细配色方案）"""
        # 设置整体背景色（浅灰，降低噪点）
        self.setStyleSheet("""
            QMainWindow {
                background-color: #F8F9FA;
            }
        """)
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(12, 12, 12, 12)
        
        # ========== A. 顶部栏 ==========
        header_frame = QFrame()
        header_frame.setStyleSheet("""
            QFrame {
                background-color: white;
                border-radius: 8px;
                padding: 15px;
            }
        """)
        header_layout = QHBoxLayout(header_frame)
        
        # 标题
        title_label = QLabel("Umi-OCR 智能重命名助手")
        title_font = QFont()
        title_font.setPointSize(18)
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setStyleSheet("color: #0078D4;")
        header_layout.addWidget(title_label)
        
        header_layout.addStretch()
        
        # 相似度阈值
        threshold_layout = QHBoxLayout()
        threshold_label = QLabel("相似度阈值:")
        threshold_label.setStyleSheet("color: #333; font-size: 12px;")
        self.threshold_slider = QSlider(Qt.Horizontal)
        self.threshold_slider.setRange(50, 100)
        self.threshold_slider.setValue(70)
        self.threshold_slider.setMinimumWidth(120)
        self.threshold_slider.valueChanged.connect(self.on_threshold_changed)
        self.threshold_value_label = QLabel("70%")
        self.threshold_value_label.setStyleSheet("color: #0078D4; font-weight: bold; min-width: 50px;")
        threshold_layout.addWidget(threshold_label)
        threshold_layout.addWidget(self.threshold_slider)
        threshold_layout.addWidget(self.threshold_value_label)
        header_layout.addLayout(threshold_layout)

        # 匹配进度总览
        self.summary_label = QLabel("进度：暂无数据")
        self.summary_label.setStyleSheet("color: #605E5C; font-size: 12px; padding: 0 10px;")
        header_layout.addWidget(self.summary_label)
        
        header_layout.addSpacing(20)
        
        # 引擎状态指示灯
        self.status_label = QLabel("✓ OCR引擎就绪" if self.ocr_controller else "✗ OCR引擎未就绪")
        self.status_label.setStyleSheet(
            "color: #107C10; font-weight: bold; padding: 5px 15px; background-color: #e8f5e9; border-radius: 5px;"
            if self.ocr_controller else
            "color: #D83B01; font-weight: bold; padding: 5px 15px; background-color: #ffebee; border-radius: 5px;"
        )
        header_layout.addWidget(self.status_label)
        
        main_layout.addWidget(header_frame)
        
        # ========== B. 三栏对比操作台（左 A | 中 操作 | 右 B）==========
        body_container = QWidget()
        body_layout = QHBoxLayout(body_container)
        body_layout.setSpacing(24)
        body_layout.setContentsMargins(0, 0, 0, 0)

        # 左侧：A 组（标准参考区）
        a_group = QGroupBox("A 组（标准参考区）")
        a_group.setMinimumWidth(480)
        a_group.setStyleSheet("""
            QGroupBox {
                font-size: 14px;
                font-weight: bold;
                border: 2px solid #0078D4;
                border-radius: 6px;
                margin-top: 8px;
                padding: 8px 10px 10px 10px;
                padding-top: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
                color: #0078D4;
            }
        """)
        a_layout = QVBoxLayout()
        a_layout.setSpacing(8)
        
        # A组选择按钮（支持文件和文件夹）
        a_btn_layout = QHBoxLayout()
        a_btn_layout.setSpacing(10)
        
        self.a_select_files_btn = QPushButton("📄 选择图片")
        self.a_select_files_btn.setStyleSheet("""
            QPushButton {
                background-color: #0078D4;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 10px 20px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
            QPushButton:pressed {
                background-color: #005a9e;
                padding-top: 11px;
                padding-left: 21px;
            }
        """)
        self.a_select_files_btn.clicked.connect(self.select_files_a)
        # 仅保留拖拽上传功能，隐藏“选择图片”按钮
        self.a_select_files_btn.setVisible(False)
        self.a_select_files_btn.setEnabled(False)
        
        self.a_select_folder_btn = QPushButton("📁 选择文件夹")
        self.a_select_folder_btn.setStyleSheet("""
            QPushButton {
                background-color: #0078D4;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 10px 20px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
            QPushButton:pressed {
                background-color: #005a9e;
                padding-top: 11px;
                padding-left: 21px;
            }
        """)
        self.a_select_folder_btn.clicked.connect(self.select_folder_a)
        # 仅保留拖拽上传功能，隐藏“选择文件夹”按钮
        self.a_select_folder_btn.setVisible(False)
        self.a_select_folder_btn.setEnabled(False)
        
        a_btn_layout.addWidget(self.a_select_files_btn)
        a_btn_layout.addWidget(self.a_select_folder_btn)
        a_layout.addLayout(a_btn_layout)
        
        # A组路径显示
        self.a_folder_label = QLabel("未选择（支持拖拽图片或文件夹到此区域）")
        self.a_folder_label.setStyleSheet("color: #666; font-size: 11px; padding: 2px 0;")
        self.a_folder_label.setWordWrap(True)
        a_layout.addWidget(self.a_folder_label)

        # A组结果过滤（全部 / 未匹配 / 已匹配）
        a_filter_layout = QHBoxLayout()
        a_filter_layout.setSpacing(6)
        a_filter_label = QLabel("显示：")
        a_filter_label.setStyleSheet("color: #666; font-size: 11px;")
        a_filter_layout.addWidget(a_filter_label)

        def make_filter_button(text: str) -> QPushButton:
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #FFFFFF;
                    color: #605E5C;
                    border: 1px solid #C8C6C4;
                    border-radius: 10px;
                    padding: 2px 10px;
                    font-size: 11px;
                }
                QPushButton:checked {
                    background-color: #0078D4;
                    color: #FFFFFF;
                    border-color: #005A9E;
                }
            """)
            return btn

        self.a_filter_all_btn = make_filter_button("全部")
        self.a_filter_unmatched_btn = make_filter_button("未匹配")
        self.a_filter_matched_btn = make_filter_button("已匹配")

        self.a_filter_group = QButtonGroup(self)
        self.a_filter_group.setExclusive(True)
        self.a_filter_group.addButton(self.a_filter_all_btn)
        self.a_filter_group.addButton(self.a_filter_unmatched_btn)
        self.a_filter_group.addButton(self.a_filter_matched_btn)
        self.a_filter_all_btn.setChecked(True)

        self.a_filter_all_btn.clicked.connect(lambda: self.set_a_filter_mode("all"))
        self.a_filter_unmatched_btn.clicked.connect(lambda: self.set_a_filter_mode("unmatched"))
        self.a_filter_matched_btn.clicked.connect(lambda: self.set_a_filter_mode("matched"))

        a_filter_layout.addWidget(self.a_filter_all_btn)
        a_filter_layout.addWidget(self.a_filter_unmatched_btn)
        a_filter_layout.addWidget(self.a_filter_matched_btn)
        a_filter_layout.addStretch()
        a_layout.addLayout(a_filter_layout)
        
        # A组搜索框
        # （已取消搜索功能，预留代码便于后续恢复）
        # a_search_layout = QHBoxLayout()
        # search_a_label = QLabel("🔍 搜索:")
        # self.a_search_edit = QLineEdit()
        # ...
        # a_layout.addLayout(a_search_layout)
        
        # A组图片卡片列表（支持拖拽，网格布局）
        self.a_scroll = QScrollArea()
        self.a_scroll.setWidgetResizable(True)
        self.a_scroll.setAcceptDrops(True)
        self.a_scroll.dragEnterEvent = self.on_a_drag_enter
        self.a_scroll.dropEvent = self.on_a_drop
        self.a_scroll.setStyleSheet("""
            QScrollArea {
                border: 1px solid #ddd;
                border-radius: 6px;
                background-color: #F3F3F3;
            }
        """)
        # 仅允许纵向滚动，禁止横向滚动条，配合自适应列数让内容自动换行
        self.a_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.a_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        
        self.a_cards_widget = QWidget()
        # 使用网格布局，实现多列排布
        self.a_cards_layout = QGridLayout(self.a_cards_widget)
        self.a_cards_layout.setSpacing(6)
        self.a_cards_layout.setContentsMargins(6, 6, 6, 6)
        # 内容始终靠左上对齐，避免图片少时居中留大空白
        self.a_cards_layout.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        
        self.a_scroll.setWidget(self.a_cards_widget)
        a_layout.addWidget(self.a_scroll)
        
        a_group.setLayout(a_layout)
        body_layout.addWidget(a_group, 1)
        
        # 中间：操作按钮区（固定宽度、卡片样式）
        center_buttons_frame = QFrame()
        center_buttons_frame.setFixedWidth(240)
        center_buttons_frame.setSizePolicy(
            QSizePolicy.Preferred, QSizePolicy.Expanding
        )
        center_buttons_frame.setStyleSheet("""
            QFrame {
                background-color: #F3F2F1;
                border: 1px solid #E1DFDD;
                border-radius: 6px;
                padding: 12px;
            }
        """)
        center_vbox = QVBoxLayout(center_buttons_frame)
        center_vbox.setSpacing(8)
        center_vbox.setContentsMargins(12, 16, 12, 16)
        center_vbox.addStretch(1)  # 顶部弹性空间，实现垂直居中

        body_layout.addWidget(center_buttons_frame, 0)
        
        # 右侧：B 组（待处理区）
        b_group = QGroupBox("B 组（待处理区）")
        b_group.setMinimumWidth(480)
        b_group.setStyleSheet("""
            QGroupBox {
                font-size: 14px;
                font-weight: bold;
                border: 2px solid #0078D4;
                border-radius: 6px;
                margin-top: 8px;
                padding: 8px 10px 10px 10px;
                padding-top: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
                color: #0078D4;
            }
        """)
        b_layout = QVBoxLayout()
        b_layout.setSpacing(8)
        
        # B组选择按钮（支持文件和文件夹）
        b_btn_layout = QHBoxLayout()
        b_btn_layout.setSpacing(10)
        
        self.b_select_files_btn = QPushButton("📄 选择图片")
        self.b_select_files_btn.setStyleSheet("""
            QPushButton {
                background-color: #0078D4;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 10px 20px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
            QPushButton:pressed {
                background-color: #005a9e;
                padding-top: 11px;
                padding-left: 21px;
            }
        """)
        self.b_select_files_btn.clicked.connect(self.select_files_b)
        # 仅保留拖拽上传功能，隐藏“选择图片”按钮
        self.b_select_files_btn.setVisible(False)
        self.b_select_files_btn.setEnabled(False)
        
        self.b_select_folder_btn = QPushButton("📁 选择文件夹")
        self.b_select_folder_btn.setStyleSheet("""
            QPushButton {
                background-color: #0078D4;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 10px 20px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
            QPushButton:pressed {
                background-color: #005a9e;
                padding-top: 11px;
                padding-left: 21px;
            }
        """)
        self.b_select_folder_btn.clicked.connect(self.select_folder_b)
        # 仅保留拖拽上传功能，隐藏“选择文件夹”按钮
        self.b_select_folder_btn.setVisible(False)
        self.b_select_folder_btn.setEnabled(False)
        
        b_btn_layout.addWidget(self.b_select_files_btn)
        b_btn_layout.addWidget(self.b_select_folder_btn)
        b_layout.addLayout(b_btn_layout)
        
        # B组路径显示
        self.b_folder_label = QLabel("未选择（支持拖拽图片或文件夹到此区域）")
        self.b_folder_label.setStyleSheet("color: #666; font-size: 11px; padding: 2px 0;")
        self.b_folder_label.setWordWrap(True)
        b_layout.addWidget(self.b_folder_label)

        # B组结果过滤（全部 / 未匹配 / 已匹配）
        b_filter_layout = QHBoxLayout()
        b_filter_layout.setSpacing(6)
        b_filter_label = QLabel("显示：")
        b_filter_label.setStyleSheet("color: #666; font-size: 11px;")
        b_filter_layout.addWidget(b_filter_label)

        self.b_filter_all_btn = QPushButton("全部")
        self.b_filter_unmatched_btn = QPushButton("未匹配")
        self.b_filter_matched_btn = QPushButton("已匹配")
        for btn in (self.b_filter_all_btn, self.b_filter_unmatched_btn, self.b_filter_matched_btn):
            btn.setCheckable(True)
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #FFFFFF;
                    color: #605E5C;
                    border: 1px solid #C8C6C4;
                    border-radius: 10px;
                    padding: 2px 10px;
                    font-size: 11px;
                }
                QPushButton:checked {
                    background-color: #0078D4;
                    color: #FFFFFF;
                    border-color: #005A9E;
                }
            """)

        self.b_filter_group = QButtonGroup(self)
        self.b_filter_group.setExclusive(True)
        self.b_filter_group.addButton(self.b_filter_all_btn)
        self.b_filter_group.addButton(self.b_filter_unmatched_btn)
        self.b_filter_group.addButton(self.b_filter_matched_btn)
        self.b_filter_all_btn.setChecked(True)

        self.b_filter_all_btn.clicked.connect(lambda: self.set_b_filter_mode("all"))
        self.b_filter_unmatched_btn.clicked.connect(lambda: self.set_b_filter_mode("unmatched"))
        self.b_filter_matched_btn.clicked.connect(lambda: self.set_b_filter_mode("matched"))

        b_filter_layout.addWidget(self.b_filter_all_btn)
        b_filter_layout.addWidget(self.b_filter_unmatched_btn)
        b_filter_layout.addWidget(self.b_filter_matched_btn)
        b_filter_layout.addStretch()
        b_layout.addLayout(b_filter_layout)
        
        # B组搜索框
        # （已取消搜索功能，预留代码便于后续恢复）
        # b_search_layout = QHBoxLayout()
        # search_b_label = QLabel("🔍 搜索:")
        # self.b_search_edit = QLineEdit()
        # ...
        # b_layout.addLayout(b_search_layout)
        
        # B组图片卡片列表（支持拖拽，网格布局）
        self.b_scroll = QScrollArea()
        self.b_scroll.setWidgetResizable(True)
        self.b_scroll.setAcceptDrops(True)
        self.b_scroll.dragEnterEvent = self.on_b_drag_enter
        self.b_scroll.dropEvent = self.on_b_drop
        self.b_scroll.setStyleSheet("""
            QScrollArea {
                border: 1px solid #ddd;
                border-radius: 6px;
                background-color: #F3F3F3;
            }
        """)
        # 仅允许纵向滚动，禁止横向滚动条，配合自适应列数让内容自动换行
        self.b_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.b_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        
        self.b_cards_widget = QWidget()
        # 使用网格布局，实现多列排布
        self.b_cards_layout = QGridLayout(self.b_cards_widget)
        self.b_cards_layout.setSpacing(6)
        self.b_cards_layout.setContentsMargins(6, 6, 6, 6)
        self.b_cards_layout.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        
        self.b_scroll.setWidget(self.b_cards_widget)
        b_layout.addWidget(self.b_scroll)
        
        b_group.setLayout(b_layout)
        body_layout.addWidget(b_group, 1)

        main_layout.addWidget(body_container, 2)

        # 中间操作区：添加操作按钮到 center_vbox（已在上面创建并加入 body_layout）
        btn_style = """
            QPushButton {
                background-color: #FFFFFF;
                color: #323130;
                border: 1px solid #A19F9D;
                border-radius: 8px;
                padding: 10px 18px;
                font-size: 13px;
                font-weight: bold;
                min-height: 44px;
                min-width: 160px;
            }
            QPushButton:hover { background-color: #f3f2f1; }
            QPushButton:pressed { background-color: #e1dfdd; padding-top: 11px; padding-left: 19px; }
            QPushButton:disabled { color: #A19F9D; border-color: #C8C6C4; }
        """
        self.clear_all_btn = QPushButton("🧹 清空已上传图片")
        self.clear_all_btn.setStyleSheet(btn_style)
        self.clear_all_btn.clicked.connect(self.clear_all_images)
        center_vbox.addWidget(self.clear_all_btn)
        self.clear_b_btn = QPushButton("🧹 只清空B组")
        self.clear_b_btn.setStyleSheet(btn_style)
        self.clear_b_btn.clicked.connect(self.clear_b_images)
        center_vbox.addWidget(self.clear_b_btn)
        
        self.delete_unmatched_b_btn = QPushButton("🗑 删除未匹配B组文件")
        self.delete_unmatched_b_btn.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                color: #A80000;
                border: 1px solid #A80000;
                border-radius: 8px;
                padding: 10px 18px;
                font-size: 13px;
                font-weight: bold;
                min-height: 44px;
                min-width: 160px;
            }
            QPushButton:hover { background-color: #f3f2f1; }
            QPushButton:pressed { background-color: #e1dfdd; padding-top: 11px; padding-left: 19px; }
            QPushButton:disabled { color: #A19F9D; border-color: #C8C6C4; }
        """)
        self.delete_unmatched_b_btn.clicked.connect(self.delete_unmatched_b_files)
        self.delete_unmatched_b_btn.setEnabled(False)
        center_vbox.addWidget(self.delete_unmatched_b_btn)
        # 选中两张图时显示匹配度
        self.pair_similarity_label = QLabel("请选择 A 组和 B 组各一张图片")
        self.pair_similarity_label.setStyleSheet("color: #605E5C; font-size: 11px; padding: 4px 0;")
        self.pair_similarity_label.setWordWrap(True)
        self.pair_similarity_label.setAlignment(Qt.AlignCenter)
        center_vbox.addWidget(self.pair_similarity_label)
        self.manual_match_btn = QPushButton("✅ 确认手动配对")
        self.manual_match_btn.setStyleSheet("""
            QPushButton {
                background-color: #0078D4;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 12px 20px;
                font-size: 13px;
                font-weight: bold;
                min-height: 44px;
            }
            QPushButton:hover { background-color: #106ebe; }
            QPushButton:pressed { background-color: #005a9e; }
            QPushButton:disabled { background-color: #A19F9D; color: #666; }
        """)
        self.manual_match_btn.clicked.connect(self.manual_match)
        self.manual_match_btn.setEnabled(False)
        center_vbox.addWidget(self.manual_match_btn)

        # 批处理总览（默认隐藏，仅多文件夹模式显示）
        self.batch_summary_title = QLabel("批处理总览")
        self.batch_summary_title.setStyleSheet("color: #323130; font-size: 12px; font-weight: bold; padding-top: 4px;")
        self.batch_summary_title.setAlignment(Qt.AlignCenter)
        self.batch_summary_title.setVisible(False)
        center_vbox.addWidget(self.batch_summary_title)

        self.batch_summary_list = QListWidget()
        self.batch_summary_list.setStyleSheet("""
            QListWidget {
                background-color: #FFFFFF;
                border: 1px solid #D1D1D1;
                border-radius: 6px;
                font-size: 11px;
            }
            QListWidget::item {
                padding: 4px 6px;
            }
            QListWidget::item:selected {
                background-color: #DEECF9;
                color: #0F548C;
            }
        """)
        self.batch_summary_list.setMinimumHeight(140)
        self.batch_summary_list.setMaximumHeight(180)
        self.batch_summary_list.itemClicked.connect(self.on_batch_summary_item_clicked)
        self.batch_summary_list.setVisible(False)
        center_vbox.addWidget(self.batch_summary_list)
        center_vbox.addStretch(1)  # 底部弹性空间，与顶部对称实现垂直居中

        # 按钮宽度适应 240px 中间区
        for btn in (self.clear_all_btn, self.clear_b_btn, self.delete_unmatched_b_btn, self.manual_match_btn):
            btn.setMaximumWidth(216)

        # 隐藏按钮（供程序内部调用）
        self.auto_match_btn = QPushButton("🚀 自动匹配")
        self.auto_match_btn.setStyleSheet("""
            QPushButton {
                background-color: #0F6CBD;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 14px 28px;
                font-size: 15px;
                font-weight: bold;
                min-height: 48px;
            }
            QPushButton:hover {
                background-color: #115EA3;
            }
            QPushButton:pressed {
                background-color: #0F548C;
                padding-top: 15px;
                padding-left: 29px;
            }
            QPushButton:disabled {
                background-color: #C8C6C4;
                color: #ffffff;
            }
        """)
        self.auto_match_btn.clicked.connect(self.auto_match_and_rename)
        self.auto_match_btn.setVisible(False)
        self.auto_match_btn.setEnabled(False)

        self.apply_rename_btn = QPushButton("💾 批量执行重命名")
        self.apply_rename_btn.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                color: #323130;
                border: 1px solid #A19F9D;
                border-radius: 8px;
                padding: 12px 24px;
                font-size: 14px;
                font-weight: bold;
                min-height: 44px;
            }
            QPushButton:hover {
                background-color: #F3F2F1;
            }
            QPushButton:pressed {
                background-color: #E1DFDD;
                padding-top: 13px;
                padding-left: 25px;
            }
            QPushButton:disabled {
                background-color: #F3F2F1;
                color: #A19F9D;
                border-color: #C8C6C4;
            }
        """)
        self.apply_rename_btn.clicked.connect(self.apply_matched_renames)
        self.apply_rename_btn.setVisible(False)
        self.apply_rename_btn.setEnabled(False)

        # ========== C. 底部操作栏（仅日志）==========
        footer_frame = QFrame()
        footer_frame.setStyleSheet("""
            QFrame {
                background-color: #f8f9fa;
                border-radius: 6px;
                padding: 10px 12px;
            }
        """)
        footer_layout = QVBoxLayout(footer_frame)
        footer_layout.setSpacing(10)

        # A/B 文本差异对比视图（已取消展示）
        self.diff_text = QTextEdit()
        self.diff_text.setReadOnly(True)
        self.diff_text.setAcceptRichText(True)
        self.diff_text.setMaximumHeight(0)
        self.diff_text.setVisible(False)

        # 进度日志
        log_label = QLabel("进度日志：")
        log_label.setStyleSheet("font-weight: bold; color: #333;")
        footer_layout.addWidget(log_label)
        
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(70)
        self.log_text.setStyleSheet("""
            QTextEdit {
                border: 1px solid #ddd;
                border-radius: 6px;
                background-color: white;
                padding: 10px;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 11px;
            }
        """)
        footer_layout.addWidget(self.log_text)
        
        main_layout.addWidget(footer_frame, 1)
        
        self.log("程序启动完成")
    
    def log(self, message: str):
        """添加日志"""
        from datetime import datetime
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{timestamp}] {message}")
        # 自动滚动到底部
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def on_threshold_changed(self, value):
        """相似度阈值改变"""
        self.threshold = value / 100.0
        self.threshold_value_label.setText(f"{value}%")
        self._update_pair_similarity_label()
        # 防抖：延迟触发重新匹配，避免拖动时频繁执行
        if getattr(self, "_threshold_timer", None):
            try:
                self._threshold_timer.stop()
            except Exception:
                pass
        self._threshold_timer = QTimer(self)
        self._threshold_timer.setSingleShot(True)
        self._threshold_timer.timeout.connect(self._on_threshold_debounced)
        self._threshold_timer.start(400)

    def _on_threshold_debounced(self):
        """相似度阈值变更后延迟重匹配"""
        if hasattr(self, "_threshold_timer"):
            self._threshold_timer = None
        if self.group_a_texts and self.group_b_texts:
            self.refresh_matching()

    def _update_pair_similarity_label(self):
        """选中 A+B 各一张时，显示当前匹配度、阈值要求及可能的匹配阻碍"""
        if not hasattr(self, "pair_similarity_label"):
            return
        if not self.selected_a_card or not self.selected_b_card:
            self.pair_similarity_label.setText("请选择 A 组和 B 组各一张图片")
            return
        a_path = self.selected_a_card.img_path
        b_path = self.selected_b_card.img_path
        a_text = self.group_a_texts.get(a_path, "") or ""
        b_text = self.group_b_texts.get(b_path, "") or ""
        if not a_text.strip() or not b_text.strip():
            self.pair_similarity_label.setText("请先完成两张图的 OCR 识别")
            return
        if FUZZYWUZZY_AVAILABLE:
            sim = fuzz.ratio(a_text, b_text) / 100.0
        else:
            import difflib
            sim = difflib.SequenceMatcher(None, a_text, b_text).ratio()
        pct = int(sim * 100)
        thresh_pct = int(self.threshold * 100)
        lines = [f"匹配度 {pct}%"]
        if sim >= self.threshold:
            lines.append(f"≥ 阈值 {thresh_pct}%（文本达标）")
        else:
            lines.append(f"< 阈值 {thresh_pct}%（需降低阈值）")
        # 尺寸比例一致：宽高比需相同（约 5% 误差）
        a_info = self.group_a_info.get(a_path, {})
        b_info = self.group_b_info.get(b_path, {})
        aw, ah = a_info.get('width', 0) or 0, a_info.get('height', 0) or 0
        bw, bh = b_info.get('width', 0) or 0, b_info.get('height', 0) or 0
        if aw and ah and bw and bh:
            ar, br = aw / ah, bw / bh
            ratio_ok = abs(ar - br) / max(ar, br, 0.001) <= 0.05
            if ratio_ok:
                lines.append("尺寸比例一致 ✓")
            else:
                lines.append("⚠ 尺寸比例不一致，自动匹配会跳过")
        # 一对一约束
        a_used = self.group_a_info.get(a_path, {}).get("used", False)
        b_matched = self.group_b_info.get(b_path, {}).get("matched", False)
        if a_used and not b_matched and sim >= self.threshold:
            lines.append("（该 A 已被其他 B 抢先匹配，可点击「确认手动配对」强制配对）")
        self.pair_similarity_label.setText("\n".join(lines))

    def refresh_matching(self):
        """重置匹配状态、按内容相似度重新匹配、刷新 UI、执行重命名"""
        if not self.group_a_texts or not self.group_b_texts:
            return
        # 过渡反馈
        self.summary_label.setText("正在更新匹配…")
        QApplication.processEvents()
        # 重置匹配状态（仅对未真正重命名的项）
        for b_path, b_info in self.group_b_info.items():
            if not b_info.get("renamed", False):
                b_info["matched"] = False
                b_info.pop("matched_a_path", None)
                b_info.pop("new_name", None)
                b_info.pop("similarity", None)
        for a_path in self.group_a_info:
            self.group_a_info[a_path]["used"] = False
        # 重新执行匹配逻辑并执行重命名
        self.run_matching_logic()
        matched_count = sum(1 for info in self.group_b_info.values() if info.get("matched"))
        self.update_a_table()
        self.update_b_table()
        self.update_buttons_state()  # 内含 update_summary
        self.apply_matched_renames()
        if matched_count == 0:
            self.log("未找到匹配，请检查：1) A/B 组是否均已完成 OCR 识别 2) 相似度阈值是否过高（当前 {}%）".format(int(self.threshold * 100)))

    def start_size_worker(self, image_paths: List[str], group: str):
        """异步读取图片尺寸并更新 UI"""
        if not image_paths:
            return
        if self.size_worker and self.size_worker.isRunning():
            self.size_worker.requestInterruption()
            self.size_worker.wait(500)
        self.size_worker = ImageSizeWorker(image_paths)
        self.size_worker.size_ready.connect(lambda p, w, h, g=group: self.on_image_size_ready(p, w, h, g))
        self.size_worker.start()

    def on_image_size_ready(self, img_path: str, width: int, height: int, group: str):
        """收到异步尺寸结果，更新 info 和卡片"""
        if group == 'a':
            if img_path in self.group_a_info:
                self.group_a_info[img_path]['width'] = width
                self.group_a_info[img_path]['height'] = height
                self.update_a_card(img_path)
        else:
            if img_path in self.group_b_info:
                self.group_b_info[img_path]['width'] = width
                self.group_b_info[img_path]['height'] = height
                self.update_b_card(img_path)

    def scan_folder(self, folder_path: str) -> List[str]:
        """扫描文件夹中的图片文件"""
        image_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tiff', '.webp', '.avif'}
        image_files = []
        
        if not os.path.exists(folder_path):
            return image_files
        
        for root, dirs, files in os.walk(folder_path):
            for file in files:
                if Path(file).suffix.lower() in image_extensions:
                    image_files.append(os.path.join(root, file))
        
        return sorted(image_files)
    
    def filter_image_files(self, file_paths: List[str]) -> List[str]:
        """过滤出图片文件"""
        image_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tiff', '.webp', '.avif'}
        image_files = []
        
        for file_path in file_paths:
            if os.path.isfile(file_path):
                if Path(file_path).suffix.lower() in image_extensions:
                    image_files.append(file_path)
            elif os.path.isdir(file_path):
                # 如果是文件夹，扫描其中的图片
                image_files.extend(self.scan_folder(file_path))
        
        return sorted(image_files)
    
    def select_files_a(self):
        """选择A组图片文件（多选）"""
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择A组图片（可多选）",
            "",
            "图片文件 (*.jpg *.jpeg *.png *.bmp *.gif *.tiff *.webp *.avif);;所有文件 (*.*)"
        )
        if files:
            # 过滤出图片文件
            image_files = self.filter_image_files(files)
            if image_files:
                self.add_images_to_group_a(image_files)
            else:
                QMessageBox.warning(self, "警告", "未找到有效的图片文件！")
    
    def select_folder_a(self):
        """选择A组文件夹"""
        folder = QFileDialog.getExistingDirectory(self, "选择A组文件夹（标准参考区）")
        if folder:
            self.select_folder_a_internal(folder)
    
    def select_files_b(self):
        """选择B组图片文件（多选）"""
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择B组图片（可多选）",
            "",
            "图片文件 (*.jpg *.jpeg *.png *.bmp *.gif *.tiff *.webp *.avif);;所有文件 (*.*)"
        )
        if files:
            # 过滤出图片文件
            image_files = self.filter_image_files(files)
            if image_files:
                self.add_images_to_group_b(image_files)
            else:
                QMessageBox.warning(self, "警告", "未找到有效的图片文件！")
    
    def select_folder_b(self):
        """选择B组文件夹"""
        folder = QFileDialog.getExistingDirectory(self, "选择B组文件夹（待处理区）")
        if folder:
            self.select_folder_b_internal(folder)
    
    def add_images_to_group_a(self, image_files: List[str]):
        """添加图片到A组（支持追加）"""
        if not self.group_a_images:
            self.group_a_images = []
        
        # 添加新图片（去重）
        for img in image_files:
            if img not in self.group_a_images:
                self.group_a_images.append(img)
        
        self.log(f"A组已添加 {len(image_files)} 张图片，共 {len(self.group_a_images)} 张")
        
        # 清空之前的识别结果（新图片）
        for img_path in image_files:
            if img_path not in self.group_a_info:
                self.group_a_info[img_path] = {
                    'text': '',
                    'width': 0,
                    'height': 0
                }
        
        # 更新表格显示
        self.update_a_table()
        
        # 异步读取图片尺寸，上传后立即展示
        self.start_size_worker(image_files, 'a')

        # 自动启动OCR识别（只识别新图片）
        if self.ocr_controller:
            new_images = [img for img in image_files if img not in self.group_a_texts]
            if new_images:
                self.start_ocr_a_specific(new_images)
    
    def add_images_to_group_b(self, image_files: List[str]):
        """添加图片到B组（支持追加）"""
        if not self.group_b_images:
            self.group_b_images = []
        
        # 添加新图片（去重）
        for img in image_files:
            if img not in self.group_b_images:
                self.group_b_images.append(img)
        
        self.log(f"B组已添加 {len(image_files)} 张图片，共 {len(self.group_b_images)} 张")
        
        # 清空之前的识别结果（新图片）
        for img_path in image_files:
            if img_path not in self.group_b_info:
                self.group_b_info[img_path] = {
                    'text': '',
                    'width': 0,
                    'height': 0,
                    'matched': False,
                    'new_name': os.path.basename(img_path),
                    'original_name': os.path.basename(img_path)  # 保存原始文件名，用于恢复
                }
        
        # 更新表格显示
        self.update_b_table()
        
        # 异步读取图片尺寸，上传后立即展示
        self.start_size_worker(image_files, 'b')

        # 自动启动OCR识别（只识别新图片）
        if self.ocr_controller:
            new_images = [img for img in image_files if img not in self.group_b_texts]
            if new_images:
                self.start_ocr_b_specific(new_images)
    
    def start_ocr_a(self):
        """启动A组OCR识别（全部图片）"""
        if not self.ocr_controller or not self.group_a_images:
            return
        
        self.log("开始识别A组图片...")
        self.a_select_files_btn.setEnabled(False)
        self.a_select_folder_btn.setEnabled(False)
        
        self.worker_a = OCRWorker(self.ocr_controller, self.group_a_images, "A组")
        self.worker_a.progress.connect(self.on_ocr_a_progress)
        self.worker_a.finished.connect(self.on_ocr_a_finished)
        self.worker_a.start()
    
    def start_ocr_a_specific(self, image_files: List[str]):
        """启动A组OCR识别（指定图片）"""
        if not self.ocr_controller or not image_files:
            return
        
        self.log(f"开始识别A组 {len(image_files)} 张新图片...")
        self.a_select_files_btn.setEnabled(False)
        self.a_select_folder_btn.setEnabled(False)
        
        self.worker_a = OCRWorker(self.ocr_controller, image_files, "A组")
        self.worker_a.progress.connect(self.on_ocr_a_progress)
        self.worker_a.finished.connect(self.on_ocr_a_finished)
        self.worker_a.start()
    
    def start_ocr_b(self):
        """启动B组OCR识别（全部图片）"""
        if not self.ocr_controller or not self.group_b_images:
            return
        
        self.log("开始识别B组图片...")
        self.b_select_files_btn.setEnabled(False)
        self.b_select_folder_btn.setEnabled(False)
        
        self.worker_b = OCRWorker(self.ocr_controller, self.group_b_images, "B组")
        self.worker_b.progress.connect(self.on_ocr_b_progress)
        self.worker_b.finished.connect(self.on_ocr_b_finished)
        self.worker_b.start()
    
    def start_ocr_b_specific(self, image_files: List[str]):
        """启动B组OCR识别（指定图片）"""
        if not self.ocr_controller or not image_files:
            return
        
        self.log(f"开始识别B组 {len(image_files)} 张新图片...")
        self.b_select_files_btn.setEnabled(False)
        self.b_select_folder_btn.setEnabled(False)
        
        self.worker_b = OCRWorker(self.ocr_controller, image_files, "B组")
        self.worker_b.progress.connect(self.on_ocr_b_progress)
        self.worker_b.finished.connect(self.on_ocr_b_finished)
        self.worker_b.start()
    
    def on_ocr_a_progress(self, img_path: str, text: str, status_msg: str):
        """A组OCR进度更新（实时）"""
        self.log(status_msg)
        
        if text:  # 有识别结果
            self.group_a_texts[img_path] = text
            
            # 更新图片信息
            try:
                with Image.open(img_path) as img:
                    width, height = img.size
                    self.group_a_info[img_path] = {
                        'text': text,
                        'width': width,
                        'height': height
                    }
            except:
                self.group_a_info[img_path] = {'text': text, 'width': 0, 'height': 0}
            
            # 实时更新卡片
            self.update_a_card(img_path)
    
    def on_ocr_a_finished(self):
        """A组OCR完成"""
        self.log("A组识别完成！")
        self.a_select_files_btn.setEnabled(True)
        self.a_select_folder_btn.setEnabled(True)
        
        # 保存到缓存
        if self.group_a_folder:
            self.ocr_cache[self.group_a_folder] = self.group_a_texts.copy()
        
        # 更新按钮状态
        self.update_buttons_state()
        # A/B 任意一侧识别完成后，如两侧都有文本则自动匹配
        self.trigger_auto_match_if_ready()
    
    def on_ocr_b_progress(self, img_path: str, text: str, status_msg: str):
        """B组OCR进度更新（实时）"""
        self.log(status_msg)
        
        if text:  # 有识别结果
            self.group_b_texts[img_path] = text
            
            # 更新图片信息
            try:
                with Image.open(img_path) as img:
                    width, height = img.size
                    # 如果已经存在，保留 original_name；否则设置
                    if img_path not in self.group_b_info:
                        self.group_b_info[img_path] = {
                            'text': text,
                            'width': width,
                            'height': height,
                            'matched': False,
                            'new_name': os.path.basename(img_path),
                            'original_name': os.path.basename(img_path)
                        }
                    else:
                        self.group_b_info[img_path].update({
                            'text': text,
                            'width': width,
                            'height': height
                        })
                        if 'original_name' not in self.group_b_info[img_path]:
                            self.group_b_info[img_path]['original_name'] = os.path.basename(img_path)
            except:
                if img_path not in self.group_b_info:
                    self.group_b_info[img_path] = {
                        'text': text,
                        'width': 0,
                        'height': 0,
                        'matched': False,
                        'new_name': os.path.basename(img_path),
                        'original_name': os.path.basename(img_path)
                    }
                else:
                    self.group_b_info[img_path].update({
                        'text': text,
                        'width': 0,
                        'height': 0
                    })
                    if 'original_name' not in self.group_b_info[img_path]:
                        self.group_b_info[img_path]['original_name'] = os.path.basename(img_path)
            
            # 实时更新卡片
            self.update_b_card(img_path)
    
    def on_ocr_b_finished(self):
        """B组OCR完成"""
        self.log("B组识别完成！")
        self.b_select_files_btn.setEnabled(True)
        self.b_select_folder_btn.setEnabled(True)
        
        # 保存到缓存
        if self.group_b_folder:
            self.ocr_cache[self.group_b_folder] = self.group_b_texts.copy()
        
        # 更新按钮状态
        self.update_buttons_state()
        # A/B 任意一侧识别完成后，如两侧都有文本则自动匹配
        try:
            self.trigger_auto_match_if_ready()
            if self.batch_mode_enabled:
                self.finalize_batch_task(success=True)
        except Exception as e:
            if self.batch_mode_enabled:
                self.finalize_batch_task(success=False, error=str(e))

    def trigger_auto_match_if_ready(self):
        """当 A/B 组都有 OCR 文本时自动触发匹配"""
        has_a = len(self.group_a_texts) > 0
        has_b = len(self.group_b_texts) > 0
        if has_a and has_b:
            self.auto_match_and_rename()
    
    def create_a_card(self, img_path: str):
        """创建A组图片卡片"""
        filename = os.path.basename(img_path)
        text = self.group_a_texts.get(img_path, "")
        
        card = ImageCard(img_path, filename, text)
        card.clicked.connect(lambda path, c=card: self.on_a_card_clicked(path))
        card.double_clicked.connect(lambda path: self.show_image_preview_dialog(path))
        card.delete_clicked.connect(self.on_a_card_delete)
        self.a_cards[img_path] = card
        return card
    
    def update_a_card(self, img_path: str):
        """更新A组图片卡片（含 OCR 文本与尺寸）"""
        if img_path in self.a_cards:
            card = self.a_cards[img_path]
            text = self.group_a_texts.get(img_path, "")
            card.update_text(text)
            info = self.group_a_info.get(img_path, {})
            w, h = info.get("width", 0) or 0, info.get("height", 0) or 0
            card.update_size(w, h)
        else:
            self.update_a_table()
    
    def update_a_table(self):
        """更新整个A组卡片列表"""
        # 按规则排序：未被使用的在前，已被使用的在后；尺寸从大到小，同尺寸按名称排序
        def sort_key_a(path: str):
            info = self.group_a_info.get(path, {})
            used = info.get("used", False)
            w = info.get("width", 0) or 0
            h = info.get("height", 0) or 0
            res = w * h
            name = os.path.basename(path).lower()
            # 未匹配(used=False) 排在前面，已匹配(used=True) 排在后面
            return (0 if not used else 1, -res, name)

        self.group_a_images = sorted(self.group_a_images, key=sort_key_a)

        # 清除所有现有卡片和网格项（如果当前选中卡片会被删掉，顺便清空选中状态）
        for card in list(self.a_cards.values()):
            if card is self.selected_a_card:
                self.selected_a_card = None
            card.deleteLater()
        self.a_cards.clear()
        while self.a_cards_layout.count():
            item = self.a_cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # 计算当前可用宽度，动态决定每行卡片数量，避免窗口变宽后间隙过大
        try:
            viewport_width = self.a_scroll.viewport().width()
        except AttributeError:
            viewport_width = self.a_cards_widget.width()
        available_width = viewport_width or max(self.width() // 2, 1)
        # 单个卡片宽度（包括边距的预估值）
        approx_card_width = 320
        cards_per_row = max(1, min(6, available_width // approx_card_width))
        visible_index = 0
        for img_path in self.group_a_images:
            info = self.group_a_info.get(img_path, {})
            used = info.get("used", False)

            # 过滤：all / unmatched / matched
            if self.a_filter_mode == "unmatched" and used:
                continue
            if self.a_filter_mode == "matched" and not used:
                continue

            row = visible_index // cards_per_row
            col = visible_index % cards_per_row
            visible_index += 1
            filename = os.path.basename(img_path)
            text = self.group_a_texts.get(img_path, "")
            width = info.get("width", 0) or 0
            height = info.get("height", 0) or 0

            card = ImageCard(img_path, filename, text)
            card.update_size(width, height)
            # A 组：若该模板已被使用，使用淡绿色底色标记；否则默认样式
            if used:
                card.set_status("matched")
            else:
                card.set_status("pending")
            card.clicked.connect(lambda path, c=card: self.on_a_card_clicked(path))
            card.double_clicked.connect(lambda path: self.show_image_preview_dialog(path))
            card.delete_clicked.connect(self.on_a_card_delete)
            self.a_cards_layout.addWidget(card, row, col)
            self.a_cards[img_path] = card
    
    def create_b_card(self, img_path: str):
        """创建B组图片卡片"""
        info = self.group_b_info.get(img_path, {})
        # 界面上的名称优先显示 new_name（匹配到A组后的目标名称），不影响真实文件路径
        filename = info.get('new_name', os.path.basename(img_path))
        text = self.group_b_texts.get(img_path, "")
        matched = info.get('matched', False)
        similarity = info.get('similarity', 0)
        
        # 如果有匹配信息，添加到文字中
        if matched and similarity > 0:
            text = f"[已匹配 {int(similarity*100)}%]\n{text}" if text else f"[已匹配 {int(similarity*100)}%]"
        
        card = ImageCard(img_path, filename, text)
        card.clicked.connect(lambda path, c=card: self.on_b_card_clicked(path))
        card.double_clicked.connect(lambda path: self.show_image_preview_dialog(path))
        card.delete_clicked.connect(self.on_b_card_delete)
        
        # 根据匹配状态设置角标与底色
        if matched:
            card.set_status("matched")
        else:
            # 已有 OCR 文本但未匹配，视为“候选/待处理”
            if text:
                card.set_status("candidate")
            else:
                card.set_status("pending")
        self.b_cards[img_path] = card
        return card
    
    def update_b_card(self, img_path: str):
        """更新B组图片卡片"""
        if img_path in self.b_cards:
            card = self.b_cards[img_path]
            info = self.group_b_info.get(img_path, {})
            text = self.group_b_texts.get(img_path, "")
            matched = info.get('matched', False)
            similarity = info.get('similarity', 0)
            
            # 如果有匹配信息，添加到文字中
            if matched and similarity > 0:
                text = f"[已匹配 {int(similarity*100)}%]\n{text}" if text else f"[已匹配 {int(similarity*100)}%]"
            
            card.update_text(text)
            w, h = info.get("width", 0) or 0, info.get("height", 0) or 0
            card.update_size(w, h)

            # 同步更新卡片标题显示的名称（使用 new_name）
            display_name = info.get('new_name', os.path.basename(img_path))
            card.name_label.setText(display_name)
            
            # 更新匹配状态角标与底色
            if matched:
                card.set_status("matched")
            else:
                if text:
                    card.set_status("candidate")
                else:
                    card.set_status("pending")
        else:
            # 如果卡片不存在，整体重建一次卡片网格
            self.update_b_table()
    
    def update_b_table(self):
        """更新整个B组卡片列表"""
        # 按规则排序：未匹配在前，已匹配在后；在未匹配中优先展示当前 A 焦点的高相似候选
        def sort_key_b(path: str):
            info = self.group_b_info.get(path, {})
            matched = info.get("matched", False)
            w = info.get("width", 0) or 0
            h = info.get("height", 0) or 0
            res = w * h
            # 同尺寸下按“显示名称”排序（即 new_name），确保匹配后的文件名顺序正确
            display_name = info.get("new_name", os.path.basename(path)).lower()
            # 排序逻辑：
            # 1) 未匹配的在前，已匹配的在后
            # 2) 在“未匹配”组内，如果当前有 A 焦点，则使用 b_suggestions 把相似度高的候选置顶
            # 3) 再按尺寸从大到小、名称排序
            if matched:
                suggestion_rank = 0  # 已匹配组内不需要提权
            else:
                suggestion_rank = self.b_suggestions.get(path, 9999)
            return (0 if not matched else 1, suggestion_rank, -res, display_name)

        self.group_b_images = sorted(self.group_b_images, key=sort_key_b)

        # 清除所有现有卡片和网格项（如果当前选中卡片会被删掉，顺便清空选中状态）
        for card in list(self.b_cards.values()):
            if card is self.selected_b_card:
                self.selected_b_card = None
            card.deleteLater()
        self.b_cards.clear()
        while self.b_cards_layout.count():
            item = self.b_cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # 计算当前可用宽度，动态决定每行卡片数量，避免窗口变宽后间隙过大
        try:
            viewport_width = self.b_scroll.viewport().width()
        except AttributeError:
            viewport_width = self.b_cards_widget.width()
        available_width = viewport_width or max(self.width() // 2, 1)
        # 使用与 A 组一致的预估宽度，形成规整栅格
        approx_card_width = 320
        cards_per_row = max(1, min(6, available_width // approx_card_width))
        visible_index = 0
        for img_path in self.group_b_images:
            info = self.group_b_info.get(img_path, {})
            matched = info.get('matched', False)

            # 过滤：all / unmatched / matched
            if self.b_filter_mode == "unmatched" and matched:
                continue
            if self.b_filter_mode == "matched" and not matched:
                continue

            row = visible_index // cards_per_row
            col = visible_index % cards_per_row
            visible_index += 1
            filename = info.get('new_name', os.path.basename(img_path))
            text = self.group_b_texts.get(img_path, "")
            similarity = info.get('similarity', 0)
            width = info.get("width", 0) or 0
            height = info.get("height", 0) or 0
            # 如果有匹配信息，添加到文字中
            if matched and similarity > 0:
                text_to_show = f"[已匹配 {int(similarity*100)}%]\\n{text}" if text else f"[已匹配 {int(similarity*100)}%]"
            else:
                text_to_show = text
            card = ImageCard(img_path, filename, text_to_show)
            card.update_size(width, height)
            card.clicked.connect(lambda path, c=card: self.on_b_card_clicked(path))
            card.double_clicked.connect(lambda path: self.show_image_preview_dialog(path))
            card.delete_clicked.connect(self.on_b_card_delete)
            # 根据匹配状态设置统一的角标与淡底色，而不是粗绿框
            if matched:
                card.set_status("matched")
            else:
                if text:
                    card.set_status("candidate")
                else:
                    card.set_status("pending")
            self.b_cards_layout.addWidget(card, row, col)
            self.b_cards[img_path] = card
    
    def on_a_card_clicked(self, img_path: str):
        """A组卡片点击事件"""
        # 如果再次点击同一张，取消选中
        if self.selected_a_card and self.selected_a_card.img_path == img_path:
            self.selected_a_card.set_selected(False)
            self.selected_a_card = None
            self.current_a_focus = None
            self.b_suggestions = {}
        else:
            # 取消之前选中的卡片
            if self.selected_a_card:
                self.selected_a_card.set_selected(False)
            # 选中当前卡片
            if img_path in self.a_cards:
                self.selected_a_card = self.a_cards[img_path]
                self.selected_a_card.set_selected(True)
                self.current_a_focus = img_path
                # 根据当前 A 文本计算 B 组相似度候选
                self.compute_b_suggestions_for_current_a()
        
        self._on_selection_changed()
        # A 组焦点变化后，防抖刷新 B 组排序
        if self._pending_update_b_timer:
            self._pending_update_b_timer.stop()
        self._pending_update_b_timer = QTimer(self)
        self._pending_update_b_timer.setSingleShot(True)
        self._pending_update_b_timer.timeout.connect(self._debounced_update_b_table)
        self._pending_update_b_timer.start(80)

    def _debounced_update_b_table(self):
        """防抖后的 B 表刷新"""
        if self._pending_update_b_timer:
            self._pending_update_b_timer = None
        self.update_b_table()

    def compute_b_suggestions_for_current_a(self):
        """基于当前选中的 A 文本，为 B 组计算相似度候选"""
        self.b_suggestions = {}
        if not self.current_a_focus:
            return
        a_text = self.group_a_texts.get(self.current_a_focus, "") or ""
        if not a_text.strip():
            return

        scores: List[Tuple[str, float]] = []
        for b_path in self.group_b_images:
            b_text = self.group_b_texts.get(b_path, "") or ""
            if not b_text.strip():
                continue
            try:
                if FUZZYWUZZY_AVAILABLE:
                    s = fuzz.ratio(a_text, b_text) / 100.0
                else:
                    import difflib
                    s = difflib.SequenceMatcher(None, a_text, b_text).ratio()
            except Exception:
                s = 0.0
            if s > 0:
                scores.append((b_path, s))

        # 取相似度最高的前若干个（例如 8 个），赋予较小 rank
        scores.sort(key=lambda x: x[1], reverse=True)
        top = scores[:8]
        for rank, (b_path, _) in enumerate(top):
            self.b_suggestions[b_path] = rank
    
    def on_b_card_clicked(self, img_path: str):
        """B组卡片点击事件"""
        # 如果再次点击同一张，取消选中
        if self.selected_b_card and self.selected_b_card.img_path == img_path:
            self.selected_b_card.set_selected(False)
            self.selected_b_card = None
        else:
            # 取消之前选中的卡片
            if self.selected_b_card:
                self.selected_b_card.set_selected(False)
            # 选中当前卡片
            if img_path in self.b_cards:
                self.selected_b_card = self.b_cards[img_path]
                self.selected_b_card.set_selected(True)
        
        self._on_selection_changed()

    def _on_selection_changed(self):
        """选中状态变化时统一刷新：按钮、匹配度显示、配对日志"""
        self.update_buttons_state()
        self.update_connection_line()

    def on_a_card_delete(self, img_path: str):
        """删除A组中的一张图片"""
        if img_path in self.group_a_images:
            self.group_a_images.remove(img_path)
        self.group_a_texts.pop(img_path, None)
        self.group_a_info.pop(img_path, None)
        if self.selected_a_card and self.selected_a_card.img_path == img_path:
            self.selected_a_card = None
        self.log(f"已从A组删除图片: {os.path.basename(img_path)}")
        # 重建网格，避免留下空洞
        self.update_a_table()
        self.update_buttons_state()
        # A组有改动后重新尝试自动匹配
        self.trigger_auto_match_if_ready()
    
    def on_b_card_delete(self, img_path: str):
        """删除B组中的一张图片"""
        if img_path in self.group_b_images:
            self.group_b_images.remove(img_path)
        self.group_b_texts.pop(img_path, None)
        self.group_b_info.pop(img_path, None)
        if self.selected_b_card and self.selected_b_card.img_path == img_path:
            self.selected_b_card = None
        self.log(f"已从B组删除图片: {os.path.basename(img_path)}")
        # 重建网格，避免留下空洞
        self.update_b_table()
        self.update_buttons_state()
        self._sync_current_folder_batch_snapshot()
        # B组有改动后重新尝试自动匹配
        self.trigger_auto_match_if_ready()
    
    def show_selected_preview(self, group: str):
        """显示选中图片的预览（在状态栏或日志中提示）"""
        if group == 'a':
            if self.selected_a_card:
                img_path = self.selected_a_card.img_path
                self.log(f"已选中A组图片: {os.path.basename(img_path)}")
        elif group == 'b':
            if self.selected_b_card:
                img_path = self.selected_b_card.img_path
                self.log(f"已选中B组图片: {os.path.basename(img_path)}")
    
    def show_image_preview_dialog(self, img_path: str):
        """显示图片预览对话框"""
        if not os.path.exists(img_path):
            QMessageBox.warning(self, "警告", "图片文件不存在！")
            return
        
        try:
            pixmap = QPixmap(img_path)
            if pixmap.isNull():
                QMessageBox.warning(self, "警告", "无法加载图片！")
                return
            
            # 创建预览窗口
            preview_dialog = QMessageBox(self)
            preview_dialog.setWindowTitle(f"图片预览 - {os.path.basename(img_path)}")
            preview_dialog.setText(f"文件名: {os.path.basename(img_path)}\n路径: {img_path}")
            
            # 缩放图片以适应屏幕
            screen_size = QApplication.primaryScreen().availableGeometry().size()
            max_width = screen_size.width() * 0.8
            max_height = screen_size.height() * 0.8
            
            scaled_pixmap = pixmap.scaled(
                max_width, max_height,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation
            )
            
            preview_dialog.setIconPixmap(scaled_pixmap)
            preview_dialog.setStandardButtons(QMessageBox.Ok)
            preview_dialog.exec()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"显示图片预览失败：{e}")
    
    def on_a_search_changed(self, text: str):
        """A组搜索过滤"""
        self.filter_table(self.a_cards_layout, text, self.a_cards)
    
    def on_b_search_changed(self, text: str):
        """B组搜索过滤"""
        self.filter_table(self.b_cards_layout, text, self.b_cards)
    
    def filter_table(self, layout: QVBoxLayout, search_text: str, cards_dict: Dict[str, ImageCard]):
        """根据搜索文本过滤卡片"""
        if not search_text:
            # 显示所有卡片
            for card in cards_dict.values():
                card.setVisible(True)
            return
        
        search_text = search_text.lower()
        for img_path, card in cards_dict.items():
            filename = os.path.basename(img_path).lower()
            text = self.group_a_texts.get(img_path, "") or self.group_b_texts.get(img_path, "")
            text = text.lower()
            
            match = search_text in filename or search_text in text
            card.setVisible(match)
    
    def on_a_drag_enter(self, event: QDragEnterEvent):
        """A组拖拽进入事件"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
    
    def on_a_drop(self, event: QDropEvent):
        """A组拖拽放下事件（支持文件和文件夹）"""
        urls = event.mimeData().urls()
        if urls:
            file_paths = [url.toLocalFile() for url in urls]
            image_files = self.filter_image_files(file_paths)
            if image_files:
                self.add_images_to_group_a(image_files)
            else:
                QMessageBox.warning(self, "警告", "拖拽的内容中没有找到有效的图片文件！")
    
    def on_b_drag_enter(self, event: QDragEnterEvent):
        """B组拖拽进入事件"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
    
    def on_b_drop(self, event: QDropEvent):
        """B组拖拽放下事件（支持文件和文件夹）"""
        urls = event.mimeData().urls()
        if urls:
            file_paths = [url.toLocalFile() for url in urls]
            folder_paths = []
            for p in file_paths:
                if os.path.isdir(p):
                    abs_p = os.path.abspath(p)
                    if abs_p not in folder_paths:
                        folder_paths.append(abs_p)

            # 多文件夹：进入“单A对多B”批处理模式
            if len(folder_paths) > 1:
                self.start_batch_process(folder_paths)
                return

            image_files = self.filter_image_files(file_paths)
            if image_files:
                self.add_images_to_group_b(image_files)
            else:
                QMessageBox.warning(self, "警告", "拖拽的内容中没有找到有效的图片文件！")

    def start_batch_process(self, b_folders: List[str]):
        """启动多B文件夹串行批处理（单A对多B）"""
        if self.batch_mode_enabled:
            QMessageBox.warning(self, "警告", "批处理正在进行中，请等待当前任务完成。")
            return
        if not self.group_a_texts:
            QMessageBox.warning(self, "警告", "请先准备并识别 A 组图片，再进行多文件夹批处理。")
            return

        valid_folders = [f for f in b_folders if os.path.isdir(f)]
        if not valid_folders:
            QMessageBox.warning(self, "警告", "未找到有效的文件夹。")
            return

        self.batch_mode_enabled = True
        self.batch_current_index = -1
        self.batch_selected_index = -1
        self.batch_results = {}
        self.batch_tasks = []
        for folder in valid_folders:
            self.batch_tasks.append({
                "b_folder": folder,
                "status": "pending",
                "matched_count": 0,
                "total_count": 0,
                "error": "",
                "snapshot": None,
            })

        self.batch_summary_title.setVisible(True)
        self.batch_summary_list.setVisible(True)
        self.update_batch_summary()
        self.log(f"开始批处理：共 {len(self.batch_tasks)} 个B组文件夹。")
        self.process_next_batch_task()

    def process_next_batch_task(self):
        """处理队列中的下一个B文件夹"""
        if not self.batch_mode_enabled:
            return

        next_index = -1
        for idx, task in enumerate(self.batch_tasks):
            if task.get("status") == "pending":
                next_index = idx
                break

        if next_index < 0:
            self.finish_batch_process()
            return

        self.batch_current_index = next_index
        self.batch_selected_index = next_index
        current_task = self.batch_tasks[next_index]
        current_task["status"] = "running"
        self.update_batch_summary()

        folder = str(current_task.get("b_folder", ""))
        self.log(f"[批处理 {next_index+1}/{len(self.batch_tasks)}] 处理中：{os.path.basename(folder)}")

        had_cache = folder in self.ocr_cache
        self.select_folder_b_internal(folder)

        # 命中缓存时不会触发 OCR 完成回调，需要在此处直接推进流程
        if had_cache:
            try:
                self.trigger_auto_match_if_ready()
                self.finalize_batch_task(success=True)
            except Exception as e:
                self.finalize_batch_task(success=False, error=str(e))

    def finalize_batch_task(self, success: bool, error: str = ""):
        """收尾当前任务并推进下一组"""
        if not self.batch_mode_enabled:
            return
        if self.batch_current_index < 0 or self.batch_current_index >= len(self.batch_tasks):
            return

        task = self.batch_tasks[self.batch_current_index]
        folder = str(task.get("b_folder", ""))
        total_count = len(self.group_b_images)
        matched_count = sum(1 for info in self.group_b_info.values() if info.get("matched", False))

        task["total_count"] = total_count
        task["matched_count"] = matched_count
        task["error"] = error
        task["status"] = "done" if success else "failed"
        task["snapshot"] = self._make_current_b_snapshot()
        self.batch_results[folder] = {
            "status": task["status"],
            "matched_count": matched_count,
            "total_count": total_count,
            "error": error,
            "snapshot": task["snapshot"],
        }
        self.update_batch_summary()

        if success:
            self.log(f"[批处理完成] {os.path.basename(folder)}：匹配 {matched_count}/{total_count}")
        else:
            self.log(f"[批处理失败] {os.path.basename(folder)}：{error}")

        QTimer.singleShot(0, self.process_next_batch_task)

    def finish_batch_process(self):
        """批处理队列全部完成"""
        done = sum(1 for t in self.batch_tasks if t.get("status") == "done")
        failed = sum(1 for t in self.batch_tasks if t.get("status") == "failed")
        self.log(f"批处理结束：成功 {done} 组，失败 {failed} 组。")
        self.batch_mode_enabled = False
        self.batch_current_index = -1
        if self.batch_selected_index < 0 and self.batch_tasks:
            self.batch_selected_index = 0
        self.update_batch_summary()

    def update_batch_summary(self):
        """刷新批处理总览列表"""
        if not hasattr(self, "batch_summary_list"):
            return
        self.batch_summary_list.clear()

        for idx, task in enumerate(self.batch_tasks):
            folder = str(task.get("b_folder", ""))
            status = str(task.get("status", "pending"))
            matched_count = int(task.get("matched_count", 0) or 0)
            # 总览比例改为：B组已匹配 / A组总数
            a_total_count = len(self.group_a_images)
            if status == "running":
                status_text = "处理中"
            elif status == "done":
                status_text = "已完成"
            elif status == "failed":
                status_text = "失败"
            else:
                status_text = "待处理"

            item_text = f"{idx+1}. {os.path.basename(folder)} | {status_text} | {matched_count}/{a_total_count}"
            self.batch_summary_list.addItem(item_text)

        if self.batch_tasks:
            self.batch_summary_title.setVisible(True)
            self.batch_summary_list.setVisible(True)
            highlight_index = self.batch_selected_index
            if highlight_index < 0:
                highlight_index = self.batch_current_index
            if highlight_index >= 0 and highlight_index < self.batch_summary_list.count():
                self.batch_summary_list.setCurrentRow(highlight_index)
        else:
            self.batch_selected_index = -1
            self.batch_summary_title.setVisible(False)
            self.batch_summary_list.setVisible(False)

    def on_batch_summary_item_clicked(self, item=None):
        """点击总览中的某组，切换到该组明细展示"""
        # 优先使用当前行，避免 item 在列表刷新后被销毁导致 RuntimeError
        row = self.batch_summary_list.currentRow()
        if row < 0 and item is not None:
            try:
                row = self.batch_summary_list.row(item)
            except RuntimeError:
                return
        if row < 0 or row >= len(self.batch_tasks):
            return
        self.batch_selected_index = row
        task = self.batch_tasks[row]
        status = str(task.get("status", "pending"))

        # 仅当“当前任务正在处理”时禁止切换，其他阶段允许查看已完成分组
        current_running = False
        if self.batch_mode_enabled:
            if self.worker_b and self.worker_b.isRunning():
                current_running = True
            elif 0 <= self.batch_current_index < len(self.batch_tasks):
                current_running = str(self.batch_tasks[self.batch_current_index].get("status", "")) == "running"
        if current_running and status not in ("done", "failed"):
            self.log("当前批处理仍在进行，请先等待该组完成后再切换。")
            return

        # 切换前先保存当前组最新状态，避免遗漏回写
        self._sync_current_folder_batch_snapshot()

        snapshot = task.get("snapshot")
        if snapshot:
            self._restore_b_snapshot(snapshot)
            self.log(f"已切换到批处理明细：{os.path.basename(str(task.get('b_folder', '')))}")
        else:
            # 懒加载兜底：无快照时按文件夹重新加载，避免点击无效
            folder = str(task.get("b_folder", ""))
            if folder and os.path.isdir(folder):
                self.select_folder_b_internal(folder)
                self._sync_current_folder_batch_snapshot()
                self.log(f"已加载分组明细：{os.path.basename(folder)}")
            else:
                self.log(f"该组尚无可展示快照：{os.path.basename(str(task.get('b_folder', '')))}")

    def _switch_to_batch_index(self, index: int) -> bool:
        """按索引切换批处理分组（用于自动切换），成功返回 True。"""
        if index < 0 or index >= len(self.batch_tasks):
            return False
        self.batch_selected_index = index
        self.batch_summary_list.setCurrentRow(index)
        task = self.batch_tasks[index]
        snapshot = task.get("snapshot")
        if snapshot:
            self._restore_b_snapshot(snapshot)
            self.log(f"已切换到批处理明细：{os.path.basename(str(task.get('b_folder', '')))}")
            return True
        folder = str(task.get("b_folder", ""))
        if folder and os.path.isdir(folder):
            self.select_folder_b_internal(folder)
            self._sync_current_folder_batch_snapshot()
            self.log(f"已加载分组明细：{os.path.basename(folder)}")
            return True
        return False

    def _make_current_b_snapshot(self) -> Dict[str, object]:
        """记录当前B组状态快照，供总览点击后恢复明细"""
        existing_images = [p for p in self.group_b_images if os.path.exists(p)]
        existing_set = set(existing_images)
        return {
            "group_b_folder": self.group_b_folder,
            "group_b_images": existing_images,
            "group_b_texts": copy.deepcopy({k: v for k, v in self.group_b_texts.items() if k in existing_set}),
            "group_b_info": copy.deepcopy({k: v for k, v in self.group_b_info.items() if k in existing_set}),
        }

    def _restore_b_snapshot(self, snapshot: Dict[str, object]):
        """恢复某个B组快照到当前明细视图"""
        self.group_b_folder = snapshot.get("group_b_folder")
        if self.group_b_folder:
            self.b_folder_label.setText(f"📁 {self.group_b_folder}")
        raw_images = list(snapshot.get("group_b_images", []))
        self.group_b_images = [p for p in raw_images if os.path.exists(p)]
        valid_set = set(self.group_b_images)
        raw_texts = copy.deepcopy(snapshot.get("group_b_texts", {}))
        raw_info = copy.deepcopy(snapshot.get("group_b_info", {}))
        self.group_b_texts = {k: v for k, v in raw_texts.items() if k in valid_set}
        self.group_b_info = {k: v for k, v in raw_info.items() if k in valid_set}
        self.selected_b_card = None
        # 切换B组时，A组“已使用”状态必须按当前组重建，避免沿用上一组状态
        self._refresh_a_used_from_current_b()
        self.update_a_table()
        self.update_b_table()
        self.update_buttons_state()

    def _refresh_a_used_from_current_b(self):
        """根据当前B组匹配关系，重建A组 used 状态（每组独立）。"""
        # 先全部重置
        for a_path, a_info in list(self.group_a_info.items()):
            if a_info.get("used"):
                a_info["used"] = False
                self.group_a_info[a_path] = a_info

        # 再根据当前B组已匹配项回填
        for b_info in self.group_b_info.values():
            if not b_info.get("matched", False):
                continue
            a_path = b_info.get("matched_a_path")
            if not a_path:
                continue
            if a_path in self.group_a_info:
                a_info = self.group_a_info.get(a_path, {})
                a_info["used"] = True
                self.group_a_info[a_path] = a_info

    def _sync_current_folder_batch_snapshot(self):
        """当前B组发生手动改动后，同步更新批处理快照，避免切组回滚到旧数据。"""
        if not self.group_b_folder or not self.batch_tasks:
            return
        target_index = -1
        for idx, task in enumerate(self.batch_tasks):
            if str(task.get("b_folder", "")) == str(self.group_b_folder):
                target_index = idx
                break
        if target_index < 0:
            return

        snapshot = self._make_current_b_snapshot()
        task = self.batch_tasks[target_index]
        task["snapshot"] = snapshot
        task["total_count"] = len(snapshot.get("group_b_images", []))
        task["matched_count"] = sum(
            1 for info in snapshot.get("group_b_info", {}).values()
            if info.get("matched", False)
        )
        folder = str(task.get("b_folder", ""))
        if folder in self.batch_results:
            self.batch_results[folder]["snapshot"] = snapshot
            self.batch_results[folder]["total_count"] = task["total_count"]
            self.batch_results[folder]["matched_count"] = task["matched_count"]
        self.update_batch_summary()
    
    def select_folder_a_internal(self, folder: str):
        """内部方法：选择A组文件夹"""
        self.group_a_folder = folder
        self.a_folder_label.setText(f"📁 {folder}")
        self.log(f"已选择A组文件夹: {folder}")
        
        self.group_a_images = self.scan_folder(folder)
        self.log(f"A组扫描到 {len(self.group_a_images)} 张图片")
        
        self.group_a_texts = {}
        self.group_a_info = {}
        # 清空卡片
        for card in list(self.a_cards.values()):
            card.deleteLater()
        self.a_cards.clear()
        self.selected_a_card = None
        
        cache_key = folder
        if cache_key in self.ocr_cache:
            self.log("使用缓存的OCR结果")
            self.group_a_texts = self.ocr_cache[cache_key].copy()
            self.update_a_table()
        else:
            self.start_ocr_a()
    
    def select_folder_b_internal(self, folder: str):
        """内部方法：选择B组文件夹"""
        self.group_b_folder = folder
        self.b_folder_label.setText(f"📁 {folder}")
        self.log(f"已选择B组文件夹: {folder}")
        
        self.group_b_images = self.scan_folder(folder)
        self.log(f"B组扫描到 {len(self.group_b_images)} 张图片")
        
        self.group_b_texts = {}
        self.group_b_info = {}
        # 清空卡片
        for card in list(self.b_cards.values()):
            card.deleteLater()
        self.b_cards.clear()
        self.selected_b_card = None
        self.matches = []

        # 选定新的 B 组时，重置 A 组的“已使用模板”状态，方便重新参与新一轮匹配
        for a_path, info in list(self.group_a_info.items()):
            if info.get("used"):
                info["used"] = False
                self.group_a_info[a_path] = info
        # A 组展示顺序也随之刷新
        self.update_a_table()
        
        cache_key = folder
        if cache_key in self.ocr_cache:
            self.log("使用缓存的OCR结果")
            self.group_b_texts = self.ocr_cache[cache_key].copy()
            self.update_b_table()
        else:
            self.start_ocr_b()
    
    def update_connection_line(self):
        """选中 A+B 各一张时记录日志，便于手动配对"""
        if self.selected_a_card and self.selected_b_card:
            a_name = os.path.basename(self.selected_a_card.img_path)
            b_name = os.path.basename(self.selected_b_card.img_path)
            self.log(f"准备配对: A组 [{a_name}] ↔ B组 [{b_name}]")
    
    def update_buttons_state(self):
        """更新按钮状态"""
        has_a = len(self.group_a_texts) > 0
        has_b = len(self.group_b_texts) > 0
        # 自动匹配改为手动触发：当 A/B 都有 OCR 结果时才允许点击
        self.auto_match_btn.setEnabled(has_a and has_b)
        
        # 手动配对：需要左右各选一项，并更新匹配度显示
        a_selected = self.selected_a_card is not None
        b_selected = self.selected_b_card is not None
        self.manual_match_btn.setEnabled(a_selected and b_selected)
        self._update_pair_similarity_label()

        # 批量重命名：当存在至少一条匹配关系时启用（matched=True）
        any_matched = any(
            info.get('matched', False)
            for info in self.group_b_info.values()
        )
        self.apply_rename_btn.setEnabled(any_matched)

        # 删除未匹配B组文件：只要存在 matched=False 的 B 图片即可启用
        has_unmatched_b = any(
            not self.group_b_info.get(p, {}).get('matched', False)
            for p in self.group_b_images
        )
        self.delete_unmatched_b_btn.setEnabled(has_unmatched_b)

        # 同步更新顶部匹配进度概览
        self.update_summary()

    def update_summary(self):
        """更新顶部匹配进度统计条"""
        total = len(self.group_b_images)
        matched = sum(
            1 for path in self.group_b_images
            if self.group_b_info.get(path, {}).get('matched', False)
        )
        if total == 0:
            self.summary_label.setText("进度：暂无数据")
        else:
            percent = int(matched * 100 / total)
            self.summary_label.setText(f"进度：已匹配 {matched}/{total}（{percent}%）")

    def set_a_filter_mode(self, mode: str):
        """设置 A 组过滤模式：all / unmatched / matched"""
        if mode not in ("all", "unmatched", "matched"):
            return
        self.a_filter_mode = mode
        self.update_a_table()

    def set_b_filter_mode(self, mode: str):
        """设置 B 组过滤模式：all / unmatched / matched"""
        if mode not in ("all", "unmatched", "matched"):
            return
        self.b_filter_mode = mode
        self.update_b_table()
    
    def run_matching_logic(self):
        """执行匹配逻辑（仅计算匹配关系，不执行重命名）"""
        used_a_matches = set()
        for b_path in self.group_b_images:
            b_info = self.group_b_info.get(b_path, {})
            if b_info.get('matched', False):
                continue
            
            b_text = self.group_b_texts.get(b_path, "")
            if not b_text or not b_text.strip():
                continue
            
            best_match_a_path = None
            best_similarity = 0

            # OCR 相似度达标 + 尺寸比例（宽高比）一致
            b_info_data = self.group_b_info.get(b_path, {})
            b_w, b_h = b_info_data.get('width', 0) or 0, b_info_data.get('height', 0) or 0

            for a_path in self.group_a_images:
                if a_path in used_a_matches:
                    continue
                
                a_text = self.group_a_texts.get(a_path, "")
                if not a_text or not a_text.strip():
                    continue

                # 尺寸比例一致：宽高比相同（允许约 5% 误差，兼容缩放图）
                a_info = self.group_a_info.get(a_path, {})
                a_w, a_h = a_info.get('width', 0) or 0, a_info.get('height', 0) or 0
                if a_w and a_h and b_w and b_h:
                    a_ratio = a_w / a_h
                    b_ratio = b_w / b_h
                    if abs(a_ratio - b_ratio) / max(a_ratio, b_ratio, 0.001) > 0.05:
                        continue

                # 计算文本相似度
                if FUZZYWUZZY_AVAILABLE:
                    similarity = fuzz.ratio(a_text, b_text) / 100.0
                else:
                    import difflib
                    similarity = difflib.SequenceMatcher(None, a_text, b_text).ratio()
                
                if similarity >= self.threshold and similarity > best_similarity:
                    best_similarity = similarity
                    best_match_a_path = a_path
            
            # 执行匹配（只记录匹配关系，不重命名）
            if best_match_a_path and best_similarity >= self.threshold:
                # 记录为“待重命名”，不立刻修改真实文件名
                a_name = Path(best_match_a_path).stem
                b_ext = Path(b_path).suffix
                new_name = f"{a_name}{b_ext}"

                # 更新数据：仅记录匹配关系
                b_info = self.group_b_info.get(b_path, {})
                b_info['matched'] = True
                b_info['similarity'] = best_similarity
                b_info['matched_a_path'] = best_match_a_path
                b_info['new_name'] = new_name
                b_info['renamed'] = False  # 标记尚未真正重命名
                self.group_b_info[b_path] = b_info

                # 标记对应A图已被使用，用于排序（放在前面）
                a_info = self.group_a_info.get(best_match_a_path, {})
                a_info['used'] = True
                self.group_a_info[best_match_a_path] = a_info

                used_a_matches.add(best_match_a_path)

    def auto_match_and_rename(self):
        """自动匹配并立即执行真实重命名"""
        if not self.group_a_texts or not self.group_b_texts:
            QMessageBox.warning(self, "警告", "请先完成A组和B组的OCR识别！")
            return
        self.log("开始自动匹配并重命名文件...")
        self.auto_match_btn.setEnabled(False)
        self.run_matching_logic()
        self.update_a_table()
        self.update_b_table()
        self.auto_match_btn.setEnabled(True)
        self.apply_matched_renames()
        matched_count = sum(1 for info in self.group_b_info.values() if info.get("matched"))
        self.log(f"自动匹配完成！已匹配 {matched_count} 张")

    def apply_matched_renames(self):
        """对已匹配的B组图片批量执行真实重命名"""
        # 找出所有“已匹配但未真正重命名”的项
        pending_items = [
            (b_path, info)
            for b_path, info in self.group_b_info.items()
            if info.get('matched', False) and not info.get('renamed', False)
        ]

        if not pending_items:
            # 没有需要重命名的项时静默返回，避免打扰用户
            return

        success_count = 0
        error_count = 0

        # 注意：在遍历过程中可能修改路径，先复制列表
        for b_path, b_info in list(pending_items):
            try:
                new_name = b_info.get('new_name', os.path.basename(b_path))
                b_dir = os.path.dirname(b_path)
                new_path = os.path.join(b_dir, new_name)

                # 检查是否有其他B组图片已经被重命名为这个目标名称
                # 如果有，先把旧的改回原名（或随机名），让新的使用目标名称
                for other_b_path, other_info in list(self.group_b_info.items()):
                    if other_b_path == b_path:
                        continue
                    if not other_info.get('renamed', False):
                        continue
                    # 检查其他B组图片的当前文件名（可能是重命名后的路径）
                    other_current_name = os.path.basename(other_b_path)
                    if other_current_name == new_name:
                        # 找到冲突：另一个B组图片已经用了这个名称
                        # 先把旧的改回原名或随机名
                        try:
                            other_original_name = other_info.get('original_name', os.path.basename(other_b_path))
                            other_dir = os.path.dirname(other_b_path)
                            
                            # 尝试恢复原名，如果原名也被占用则用随机名
                            restore_path = os.path.join(other_dir, other_original_name)
                            if os.path.exists(restore_path) and restore_path != other_b_path:
                                # 原名被占用，用随机名
                                from time import time as _time
                                ext = Path(other_original_name).suffix
                                base = Path(other_original_name).stem
                                rand_token = str(int(_time() * 1000))[-6:]
                                restore_path = os.path.join(other_dir, f"{base}_restored_{rand_token}{ext}")
                                counter_restore = 1
                                original_restore_path = restore_path
                                while os.path.exists(restore_path) and restore_path != other_b_path:
                                    restore_path = os.path.join(
                                        other_dir,
                                        f"{Path(original_restore_path).stem}_{counter_restore}{ext}"
                                    )
                                    counter_restore += 1
                            
                            if os.path.exists(other_b_path) and other_b_path != restore_path:
                                os.rename(other_b_path, restore_path)
                            
                            # 更新旧图片的路径和数据
                            if other_b_path in self.group_b_images:
                                idx_old = self.group_b_images.index(other_b_path)
                                self.group_b_images[idx_old] = restore_path
                            if other_b_path in self.group_b_texts:
                                self.group_b_texts[restore_path] = self.group_b_texts.pop(other_b_path)
                            
                            info_old = self.group_b_info.pop(other_b_path)
                            info_old['matched'] = False
                            info_old['matched_a_path'] = None
                            info_old['new_name'] = os.path.basename(restore_path)
                            info_old['renamed'] = True
                            self.group_b_info[restore_path] = info_old
                            
                            # 更新卡片
                            if other_b_path in self.b_cards:
                                old_card_other = self.b_cards.pop(other_b_path)
                                old_card_other.deleteLater()
                                self.create_b_card(restore_path)
                            
                            self.log(f"🔄 释放旧配对：{os.path.basename(other_b_path)} → {os.path.basename(restore_path)}")
                        except Exception as e:
                            self.log(f"⚠ 释放旧配对失败: {os.path.basename(other_b_path)}: {e}")

                # 如果目标路径仍然被占用（文件系统中存在但不是我们管理的B组图片），才加后缀
                counter = 1
                original_new_path = new_path
                while os.path.exists(new_path) and new_path != b_path:
                    name_without_ext = Path(original_new_path).stem
                    ext = Path(original_new_path).suffix
                    new_path = os.path.join(b_dir, f"{name_without_ext}_{counter}{ext}")
                    counter += 1

                if new_path != b_path:
                    os.rename(b_path, new_path)

                    # 更新B组路径相关数据
                    if b_path in self.group_b_images:
                        idx = self.group_b_images.index(b_path)
                        self.group_b_images[idx] = new_path

                    if b_path in self.group_b_texts:
                        self.group_b_texts[new_path] = self.group_b_texts.pop(b_path)

                    if b_path in self.group_b_info:
                        info = self.group_b_info.pop(b_path)
                        # 如果是第一次重命名，保存原始文件名
                        if 'original_name' not in info:
                            info['original_name'] = os.path.basename(b_path)
                        info['new_name'] = os.path.basename(new_path)
                        info['renamed'] = True
                        self.group_b_info[new_path] = info

                    # 卡片更新：删掉旧卡片，创建新卡片
                    if b_path in self.b_cards:
                        old_card = self.b_cards.pop(b_path)
                        old_card.deleteLater()
                        self.create_b_card(new_path)

                    success_count += 1
                    self.log(f"✅ 重命名成功：{os.path.basename(b_path)} → {os.path.basename(new_path)}")
                else:
                    self.log(f"跳过：{os.path.basename(b_path)}（名称相同）")
            except Exception as e:
                error_count += 1
                self.log(f"❌ 重命名失败 {os.path.basename(b_path)}: {e}")

        # 重建A/B组卡片显示（A组已使用模板提前、高亮；B组重命名后保持分组排序）
        self.update_a_table()
        self.update_b_table()
        self.update_buttons_state()
        self._sync_current_folder_batch_snapshot()

        # 不再弹出确认或完成对话框，仅在日志中提示结果，执行过程完全自动化
        self.log(f"批量重命名完成：成功 {success_count} 张，失败 {error_count} 张")
    
    def manual_match(self):
        """确认手动配对"""
        if not self.selected_a_card or not self.selected_b_card:
            QMessageBox.warning(self, "警告", "请分别在A组和B组各选择一张图片！")
            return
        
        a_path = self.selected_a_card.img_path
        b_path = self.selected_b_card.img_path
        
        if not a_path or not b_path:
            return

        # 执行重命名：当前这对 A-B 将成为“唯一合法配对”
        a_name = Path(a_path).stem
        b_ext = Path(b_path).suffix
        new_name = f"{a_name}{b_ext}"
        
        try:
            b_dir = os.path.dirname(b_path)
            new_path = os.path.join(b_dir, new_name)
            
            # 检查是否有其他B组图片已经被重命名为这个目标名称
            # 如果有，先把旧的改回原名（或随机名），让新的使用目标名称
            for other_b_path, other_info in list(self.group_b_info.items()):
                if other_b_path == b_path:
                    continue
                if not other_info.get('renamed', False):
                    continue
                # 检查其他B组图片的当前文件名（可能是重命名后的路径）
                other_current_name = os.path.basename(other_b_path)
                if other_current_name == new_name:
                    # 找到冲突：另一个B组图片已经用了这个名称
                    # 先把旧的改回原名或随机名
                    try:
                        other_original_name = other_info.get('original_name', os.path.basename(other_b_path))
                        other_dir = os.path.dirname(other_b_path)
                        
                        # 尝试恢复原名，如果原名也被占用则用随机名
                        restore_path = os.path.join(other_dir, other_original_name)
                        if os.path.exists(restore_path) and restore_path != other_b_path:
                            # 原名被占用，用随机名
                            from time import time as _time
                            ext = Path(other_original_name).suffix
                            base = Path(other_original_name).stem
                            rand_token = str(int(_time() * 1000))[-6:]
                            restore_path = os.path.join(other_dir, f"{base}_restored_{rand_token}{ext}")
                            counter_restore = 1
                            original_restore_path = restore_path
                            while os.path.exists(restore_path) and restore_path != other_b_path:
                                restore_path = os.path.join(
                                    other_dir,
                                    f"{Path(original_restore_path).stem}_{counter_restore}{ext}"
                                )
                                counter_restore += 1
                        
                        if os.path.exists(other_b_path) and other_b_path != restore_path:
                            os.rename(other_b_path, restore_path)
                        
                        # 更新旧图片的路径和数据
                        if other_b_path in self.group_b_images:
                            idx_old = self.group_b_images.index(other_b_path)
                            self.group_b_images[idx_old] = restore_path
                        if other_b_path in self.group_b_texts:
                            self.group_b_texts[restore_path] = self.group_b_texts.pop(other_b_path)
                        
                        info_old = self.group_b_info.pop(other_b_path)
                        info_old['matched'] = False
                        info_old['matched_a_path'] = None
                        info_old['new_name'] = os.path.basename(restore_path)
                        info_old['renamed'] = True
                        self.group_b_info[restore_path] = info_old
                        
                        # 更新卡片
                        if other_b_path in self.b_cards:
                            old_card_other = self.b_cards.pop(other_b_path)
                            old_card_other.deleteLater()
                            self.create_b_card(restore_path)
                        
                        self.log(f"🔄 释放旧配对：{os.path.basename(other_b_path)} → {os.path.basename(restore_path)}")
                    except Exception as e:
                        self.log(f"⚠ 释放旧配对失败: {os.path.basename(other_b_path)}: {e}")
            
            # 如果目标路径仍然被占用（文件系统中存在但不是我们管理的B组图片），才加后缀
            counter = 1
            original_new_path = new_path
            while os.path.exists(new_path) and new_path != b_path:
                name_without_ext = Path(original_new_path).stem
                ext = Path(original_new_path).suffix
                new_path = os.path.join(b_dir, f"{name_without_ext}_{counter}{ext}")
                counter += 1
            
            if new_path != b_path:
                os.rename(b_path, new_path)
                
                # 更新数据
                old_b_path = b_path
                if old_b_path in self.group_b_images:
                    idx = self.group_b_images.index(old_b_path)
                    self.group_b_images[idx] = new_path
                
                if old_b_path in self.group_b_texts:
                    self.group_b_texts[new_path] = self.group_b_texts.pop(old_b_path)
                
                if old_b_path in self.group_b_info:
                    b_info = self.group_b_info.pop(old_b_path)
                    # 如果是第一次重命名，保存原始文件名
                    if 'original_name' not in b_info:
                        b_info['original_name'] = os.path.basename(old_b_path)
                    # 标记当前 B 为与 A 的正式配对
                    b_info['matched'] = True
                    b_info['matched_a_path'] = a_path
                    b_info['new_name'] = os.path.basename(new_path)
                    b_info['renamed'] = True
                    self.group_b_info[new_path] = b_info

                # 标记对应A图已被使用，用于排序（放在前面）
                a_info = self.group_a_info.get(a_path, {})
                a_info['used'] = True
                self.group_a_info[a_path] = a_info

                # 保证“一对一”：如果之前已经有别的 B 图匹配了同一个 A，
                # 则这些旧的 B 图需要让位（取消匹配，必要时改回一个随机名称）
                from time import time as _time
                for other_b_path, other_info in list(self.group_b_info.items()):
                    if other_b_path == new_path:
                        continue
                    if not other_info.get('matched'):
                        continue
                    if other_info.get('matched_a_path') != a_path:
                        continue

                    try:
                        # 如果之前那张 B 已经真正被重命名过（renamed=True），
                        # 则给它改成一个带随机后缀的名字，避免继续占用 A 的名称。
                        if other_info.get('renamed'):
                            other_dir = os.path.dirname(other_b_path)
                            ext = Path(other_b_path).suffix
                            base = Path(other_b_path).stem
                            rand_token = str(int(_time() * 1000))[-6:]
                            alt_path = os.path.join(other_dir, f"{base}_old_{rand_token}{ext}")
                            counter2 = 1
                            original_alt_path = alt_path
                            while os.path.exists(alt_path) and alt_path != other_b_path:
                                alt_path = os.path.join(
                                    other_dir,
                                    f"{Path(original_alt_path).stem}_{counter2}{ext}"
                                )
                                counter2 += 1

                            if os.path.exists(other_b_path) and other_b_path != alt_path:
                                os.rename(other_b_path, alt_path)

                            # 更新列表与映射中的路径
                            if other_b_path in self.group_b_images:
                                idx2 = self.group_b_images.index(other_b_path)
                                self.group_b_images[idx2] = alt_path
                            if other_b_path in self.group_b_texts:
                                self.group_b_texts[alt_path] = self.group_b_texts.pop(other_b_path)

                            info2 = self.group_b_info.pop(other_b_path)
                            info2['matched'] = False
                            info2['matched_a_path'] = None
                            info2['new_name'] = os.path.basename(alt_path)
                            info2['renamed'] = True
                            self.group_b_info[alt_path] = info2
                        else:
                            # 只是在候选列表中标记为匹配，但文件尚未改名：直接取消匹配即可
                            other_info['matched'] = False
                            other_info['matched_a_path'] = None
                            other_info['new_name'] = os.path.basename(other_b_path)
                            self.group_b_info[other_b_path] = other_info
                    except Exception as e:
                        self.log(f"⚠ 释放旧配对失败: {os.path.basename(other_b_path)}: {e}")
                
                # 更新卡片与排序，高亮 A 组模板 & B 组匹配项
                if old_b_path in self.b_cards:
                    old_card = self.b_cards.pop(old_b_path)
                    old_card.deleteLater()
                    # 创建新卡片
                    self.create_b_card(new_path)
                    if self.selected_b_card == old_card:
                        self.selected_b_card = self.b_cards.get(new_path)
                else:
                    # 如果卡片不存在，重新创建所有卡片
                    self.update_b_table()

                # 重新构建A/B卡片，使“已使用模板 / 已匹配项”靠前并有绿色标志
                self.update_a_table()
                self.update_b_table()
                # 每次确认配对后清空当前选中，避免仍然锁定在上一组导致无法重新选择
                if self.selected_a_card:
                    self.selected_a_card = None
                if self.selected_b_card:
                    self.selected_b_card = None
                self.update_buttons_state()
                self._sync_current_folder_batch_snapshot()

                self.log(f"✓ 手动配对成功: {os.path.basename(b_path)} → {new_name}")
            else:
                # 名称已相同：无需改名，但仍应视为成功配对，避免被“删除未匹配”误删
                if b_path in self.group_b_info:
                    b_info_same = self.group_b_info[b_path]
                else:
                    b_info_same = {}
                if 'original_name' not in b_info_same:
                    b_info_same['original_name'] = os.path.basename(b_path)
                b_info_same['matched'] = True
                b_info_same['matched_a_path'] = a_path
                b_info_same['new_name'] = os.path.basename(b_path)
                b_info_same['renamed'] = True
                self.group_b_info[b_path] = b_info_same

                # 标记对应A图已使用
                a_info = self.group_a_info.get(a_path, {})
                a_info['used'] = True
                self.group_a_info[a_path] = a_info

                # 保证一对一：释放其他占用同一A的B图
                for other_b_path, other_info in list(self.group_b_info.items()):
                    if other_b_path == b_path:
                        continue
                    if other_info.get('matched') and other_info.get('matched_a_path') == a_path:
                        other_info['matched'] = False
                        other_info['matched_a_path'] = None
                        other_info['new_name'] = os.path.basename(other_b_path)
                        self.group_b_info[other_b_path] = other_info

                self.update_a_table()
                self.update_b_table()
                self.update_buttons_state()
                self._sync_current_folder_batch_snapshot()
                self.log(f"✓ 手动配对成功: {os.path.basename(b_path)} → {new_name}（名称相同，已标记匹配）")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"重命名失败：{e}")
            self.log(f"✗ 手动配对失败: {e}")

    def resizeEvent(self, event):
        """窗口尺寸改变时，防抖后重排网格，避免频繁重绘"""
        super().resizeEvent(event)
        if getattr(self, "_resize_debounce_timer", None):
            try:
                self._resize_debounce_timer.stop()
            except Exception:
                pass
        self._resize_debounce_timer = QTimer(self)
        self._resize_debounce_timer.setSingleShot(True)
        self._resize_debounce_timer.timeout.connect(self._on_resize_debounced)
        self._resize_debounce_timer.start(150)

    def _on_resize_debounced(self):
        """防抖后的网格重排"""
        if hasattr(self, "_resize_debounce_timer"):
            self._resize_debounce_timer = None
        self.update_a_table()
        self.update_b_table()

    def clear_b_images(self):
        """只清空当前B组图片与匹配结果，不影响A组或其他分组"""
        current_folder = self.group_b_folder
        removed_batch_index = -1

        # 清空 B 组基础数据
        self.group_b_images = []
        self.group_b_texts = {}
        self.group_b_info = {}
        self.selected_b_card = None
        # B 组相关匹配结果也一并清理
        self.matches = []

        # 清空 B 组卡片组件
        for card in list(self.b_cards.values()):
            card.deleteLater()
        self.b_cards.clear()
        while self.b_cards_layout.count():
            item = self.b_cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # 清理当前 B 组的 OCR 缓存
        if current_folder and current_folder in self.ocr_cache:
            self.ocr_cache.pop(current_folder, None)

        # 仅清空当前组，不影响分组上下文
        self.group_b_folder = current_folder
        if current_folder:
            self.b_folder_label.setText(f"📁 {current_folder}（已清空）")
        else:
            self.b_folder_label.setText("未选择（支持拖拽图片或文件夹到此区域）")

        # A 组使用状态应基于“当前组”重建
        self._refresh_a_used_from_current_b()
        self.update_a_table()

        # 更新按钮状态（没有 B 组时不能匹配 / 批量重命名）
        self.update_buttons_state()
        # 若处于多组模式，仅更新当前组快照，不影响其他组
        self._sync_current_folder_batch_snapshot()

        # 批处理总览中移除该组，并自动切到下一组
        if current_folder and self.batch_tasks:
            for idx, task in enumerate(self.batch_tasks):
                if str(task.get("b_folder", "")) == str(current_folder):
                    removed_batch_index = idx
                    break
            if removed_batch_index >= 0:
                self.batch_tasks.pop(removed_batch_index)
                self.batch_results.pop(str(current_folder), None)
                if self.batch_current_index == removed_batch_index:
                    self.batch_current_index = -1
                elif self.batch_current_index > removed_batch_index:
                    self.batch_current_index -= 1

                # 优先切换到“删除项后面的下一组”，若不存在则切到上一组
                if self.batch_tasks:
                    next_index = min(removed_batch_index, len(self.batch_tasks) - 1)
                    self.batch_selected_index = next_index
                else:
                    self.batch_selected_index = -1
                    self.group_b_folder = None
                    self.b_folder_label.setText("未选择（支持拖拽图片或文件夹到此区域）")
                    self.batch_mode_enabled = False
                self.update_batch_summary()
                if self.batch_tasks and self.batch_selected_index >= 0:
                    self._switch_to_batch_index(self.batch_selected_index)
                self.log("已清空并移除当前 B 组。")
                return

        self.log("已清空当前 B 组图片与匹配结果。")

    def delete_unmatched_b_files(self):
        """删除B组中未匹配成功（matched=False）的图片文件（同时移除界面数据）"""
        # 避免删除过程中 OCR/尺寸读取还在访问文件
        if (self.worker_b and self.worker_b.isRunning()) or (self.size_worker and self.size_worker.isRunning()):
            QMessageBox.warning(self, "警告", "识别/读取进行中，请稍后再删除未匹配文件。")
            return

        unmatched_paths = [
            p for p in self.group_b_images
            if not self.group_b_info.get(p, {}).get("matched", False)
        ]
        if not unmatched_paths:
            QMessageBox.information(self, "提示", "当前没有未匹配的B组文件。")
            return

        delete_ok = 0
        delete_failed: List[str] = []

        # 先从磁盘尝试删除；无论成功与否，都移除界面数据，避免再次触发匹配
        for p in unmatched_paths:
            if os.path.exists(p):
                try:
                    os.remove(p)
                    delete_ok += 1
                except Exception as e:
                    delete_failed.append(f"{os.path.basename(p)}: {e}")

            # 移除界面/数据结构中的引用
            if p in self.group_b_images:
                self.group_b_images.remove(p)
            self.group_b_texts.pop(p, None)
            self.group_b_info.pop(p, None)
            self.b_suggestions.pop(p, None)

            if self.selected_b_card and self.selected_b_card.img_path == p:
                self.selected_b_card = None

        # 直接重建卡片网格
        self.update_b_table()
        self.update_buttons_state()
        # 批处理模式下，删除后需要同步当前组快照，避免切换时恢复到旧路径
        self._sync_current_folder_batch_snapshot()

        self.log(f"已删除未匹配B组文件：{delete_ok}/{len(unmatched_paths)}")
        if delete_failed:
            QMessageBox.warning(
                self,
                "删除失败",
                "部分文件删除失败（最多显示20条）：\n\n" + "\n".join(delete_failed[:20])
            )

    def clear_all_images(self):
        """清空A/B两组已上传的图片与匹配结果，恢复到初始状态"""
        self.batch_mode_enabled = False
        self.batch_current_index = -1
        self.batch_selected_index = -1
        self.batch_tasks = []
        self.batch_results = {}
        self.update_batch_summary()

        # 清空路径与基础数据
        self.group_a_folder = None
        self.group_b_folder = None
        self.group_a_images = []
        self.group_b_images = []
        self.group_a_texts = {}
        self.group_b_texts = {}
        self.group_a_info = {}
        self.group_b_info = {}
        self.matches = []
        self.selected_a_card = None
        self.selected_b_card = None

        # 清空卡片组件
        for card in list(self.a_cards.values()):
            card.deleteLater()
        self.a_cards.clear()
        while self.a_cards_layout.count():
            item = self.a_cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for card in list(self.b_cards.values()):
            card.deleteLater()
        self.b_cards.clear()
        while self.b_cards_layout.count():
            item = self.b_cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # 重置OCR缓存（彻底重新开始）
        self.ocr_cache = {}

        # 恢复标签提示文本
        self.a_folder_label.setText("未选择（支持拖拽图片或文件夹到此区域）")
        self.b_folder_label.setText("未选择（支持拖拽图片或文件夹到此区域）")

        # 禁用相关操作按钮
        self.update_buttons_state()

        self.log("已清空所有已上传图片与匹配结果，可以重新拖拽图片/文件夹开始。")

    def closeEvent(self, event):
        """窗口关闭事件：先优雅停止后台线程和OCR进程，避免闪退"""
        # 1. 请求并等待 OCR 工作线程安全退出，防止 "QThread: Destroyed while thread is still running"
        for worker in (self.worker_a, self.worker_b):
            try:
                if worker and worker.isRunning():
                    worker.requestInterruption()
                    # 最长等待3秒结束当前图片识别
                    worker.wait(3000)
            except Exception as e:
                print(f"[关闭] 停止OCR线程时出错: {e}")

        # 2. 停止 OCR 引擎子进程
        if self.ocr_controller:
            self.ocr_controller.stop()

        # 3. 清理 AVIF/HEIC 转换缓存文件
        clear_image_conversion_cache()

        # 4. 正常关闭窗口
        event.accept()


def main():
    app = QApplication(sys.argv)
    
    # 设置应用样式
    app.setStyle("Fusion")
    
    window = OCRImageMatcher()
    # 默认放在 (100, 100) 并尽量充满屏幕
    window.move(100, 100)
    window.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    # PyInstaller onefile + Windows 下，多进程 / 子进程场景的保护
    # 避免某些情况下程序在启动时被反复拉起自身
    import multiprocessing
    multiprocessing.freeze_support()
    main()
