# macOS 免 Hammerspoon 收集

Workless 的整理与提醒能力不依赖 Hammerspoon。Hammerspoon 只是“从某个 macOS App 抓取文字”的一种入口。

## 最快：浏览器快速收集

启动服务后打开：

```text
http://127.0.0.1:8787/quick-capture
```

粘贴或输入文字后点击“加入 Workless”。这条路径不依赖 Hammerspoon，也不要求从系统读取选中文字。

## macOS 快捷指令：从支持的 App 分享文字

macOS 自带“快捷指令”可以从分享菜单或服务菜单接收文本。创建一次后，用户无需安装 Hammerspoon。

1. 打开“快捷指令”，新建名为“发送到 Workless”的快捷指令。
2. 在“详细信息”中开启“在共享表单中显示”或“用作快速操作”，输入类型选择“文本”；可按需指定键盘快捷键。
3. 依次添加以下动作：
   - “生成 UUID”；
   - “当前日期”；
   - “字典”，填写：`id` = `shortcut-` 加 UUID，`content` = 快捷指令输入，`captured_at` = 当前日期，`source` = `macOS 快捷指令`，`status` = `inbox`；
   - “获取 URL 内容”，URL 填 `http://127.0.0.1:8787/captures`，方法选择 `POST`，请求正文选择 `JSON`，传入上一步字典。
4. 启动 Workless 后，在支持分享文本的 App 中选中文字，选择“共享”或“服务”里的“发送到 Workless”。

### 限制与兜底

并非所有 macOS App 都会把选中文字传给快捷指令。遇到这种 App，复制文字后打开浏览器快速收集页即可。若需要跨多数 App 的全局快捷键自动捕获，Hammerspoon 仍然是可选的增强入口。

参考：Apple 的[快捷指令输入类型](https://support.apple.com/guide/shortcuts-mac/input-types-apd7644168e1/mac)和[在其他 App 中运行快捷指令](https://support.apple.com/en-gb/guide/shortcuts-mac/-apd163eb9f95/mac)。
