# macOS 版本

在 macOS 上使用 python.org Python 3.11、Pillow 和 Xcode 命令行工具，在项目根目录运行：

```sh
python3 macos/build.py
```

生成 `dist/叠声.app`。双击后，叠声在菜单栏运行，自动启动局域网服务并打开工作区；菜单可复制地址、复制群主密钥、查看日志和重启服务。退出应用会停止服务。默认数据存储在 `~/Library/Application Support/Diesheng`；在构建机器上若已有 `build/local-workspace`，首次启动会沿用该目录。数据目录会记在用户偏好中，后续不会自动切换。

当前构建面向 Apple 芯片 Mac，内置 Python 运行时，使用临时签名，未经过 Apple 开发者签名或公证。复制到其他 Mac 时，系统可能阻止打开；需要完成正式签名和公证才能作为面向其他用户的发行版本。服务运行期间请保持应用打开。端口 7810 已被占用时，先停止占用该端口的服务，再从菜单栏重新启动。
