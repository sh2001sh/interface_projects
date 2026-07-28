# 生成产物编译说明

本文说明 `08_code_generation` 接口生成的 Qt/C++ 工程如何在本机完成编译。

## 生成产物结构

接口 `POST /api/code_generation/generate` 生成的是一个 `qmake` 工程，产物目录下会包含：

- `peach.pro`：工程入口
- `main.cpp`
- `messageconvert.cpp/.h`
- `codec.cpp/.h`
- 协议结构体与转换逻辑源码

编译时应进入单独的构建目录执行 `qmake` 和 `make`，不要直接在生成目录里原地编译。

## 编译依赖

当前已在本机验证通过的依赖为 Qt 5 工具链：

```bash
sudo apt-get update
sudo apt-get install -y build-essential qtbase5-dev qt5-qmake
```

可执行检查：

```bash
qmake -v
make -v
```

## 编译步骤

假设代码生成接口输出目录为 `OUTPUT_DIR`：

```bash
mkdir -p "${OUTPUT_DIR}_build"
cd "${OUTPUT_DIR}_build"
qmake "../$(basename "${OUTPUT_DIR}")/peach.pro"
make -j"$(nproc)"
```

编译成功后，可执行文件会生成在构建目录下，名称通常为目标转换工程名，例如：

```bash
${OUTPUT_DIR}_build/temp_sensor_to_temp_report
```

## 已验证示例

2026-05-07 已在本机完成一轮真实生成和编译验证。

生成目录：

```bash
/nfs/615/interface_projects/test/output/codegen_compile_verify_20260507_v2
```

构建目录：

```bash
/nfs/615/interface_projects/test/output/codegen_compile_verify_20260507_v2_build
```

实际执行命令：

```bash
cd /nfs/615/interface_projects/test/output/codegen_compile_verify_20260507_v2_build
qmake ../codegen_compile_verify_20260507_v2/peach.pro
make -j2
```

已生成可执行文件：

```bash
/nfs/615/interface_projects/test/output/codegen_compile_verify_20260507_v2_build/temp_sensor_to_temp_report
```

## 常见问题

1. `qmake: command not found`

缺少 `qt5-qmake`，安装 Qt 5 开发工具链后重试。

2. Qt 头文件缺失

缺少 `qtbase5-dev`，或当前系统 Qt 开发环境未完整安装。

3. 直接在生成目录执行 `make`

建议始终使用独立的 `_build` 目录，避免污染生成源码目录，也便于重复构建。
