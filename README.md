# Mod 反向导入器

这是一个 Blender 插件，用于把已经由 WWMI Tools 导出的 mod 文件夹重新导入为可编辑网格。

## 导入内容

- Position 缓冲区：顶点坐标、自定义法线、切线来源记录。
- Blend 缓冲区：顶点组和权重。
- TexCoord 缓冲区：UV 层，以及存在时的顶点颜色数据。
- Index/Component 缓冲区：网格面，可按 `.ini` 中的 `drawindexed` 片段拆分。
- WWMI 共享缓冲区：支持 `Meshes/Position.buf`、`Index.buf`、`Vector.buf`、`Texcoord.buf`、`Color.buf`、`Blend.buf`。

贴图不会处理。

## 安装

1. 在 Blender 中打开 `编辑 > 偏好设置 > 插件`。
2. 点击 `安装...`。
3. 将 `Mod_Reverse_Importer` 文件夹打包成 zip 后选择安装。
4. 启用 `Mod 反向导入器`。

## 使用

- 通过 `文件 > 导入 > Mod 文件夹（.ini）` 选择 mod 的 `.ini`。
- 或在 3D 视图右侧 `N` 面板中打开 `反向导入` 标签页。

推荐起始设置：

- `游戏预设`：请手动选择对应类型，例如 `原神（GIMI）`、`绝区零（ZZMI）`、`鸣潮（WWMI）`。
- `按 DrawIndexed 拆分`：建议开启。
- `翻转面朝向`：只有模型内外反了再开启。
- `翻转 UV V 轴`：只有 UV 上下颠倒时再开启。

## 说明

- 插件直接读取已导出的 mod 文件夹，不需要 dump 文件夹。
- 不要求 `.fmt` 文件。
- 支持 `.ib`，也支持 `Buffer/*-Component*.buf` 这类索引缓冲。
- 支持 WWMI Tools 的共享缓冲格式。
- 如果 `.ini` 中存在未被实际 `drawindexed` 引用的索引资源，插件会跳过它们，避免生成不存在或错误的部件。
