# 前端回归测试

这些用例在 jsdom 里加载真实前端代码（不打桩业务逻辑），并访问真实服务端接口，
用于锁住那些"只在浏览器里才出现、单元测试测不到"的问题。

## 前置条件

1. 服务已启动：`python3 server.py --port 8848`（默认端口，可用 `AD_BASE` 覆盖）
2. 已安装 jsdom：`npm i jsdom`，或全局安装后在运行时指定 `NODE_PATH`

```bash
# 在项目根目录
npm i jsdom
node tests/ui/create_tracker_sync.js

# 若 jsdom 装在别处
NODE_PATH=/path/to/node_modules node tests/ui/create_tracker_sync.js

# 服务不在默认端口
AD_BASE=http://127.0.0.1:9000 node tests/ui/create_tracker_sync.js
```

缺少 jsdom 或服务未启动时会打印原因并直接跳过，不会误报失败。

## 用例

| 文件 | 覆盖内容 |
| --- | --- |
| `create_tracker_sync.js` | 新建跟踪任务的表单状态同步：输入框有值但不触发 blur、创建后表单残留、改代码后沿用旧名称、成本假设是否保留 |

## 为什么需要它

后端单元测试（`tests/test_*.py`）与接口审计（`tests/audit_json_endpoints.py`）
覆盖不到浏览器侧的表单与渲染行为。此前几个真实故障都属于这一类：

- 服务端返回 `Infinity` 导致浏览器 `JSON.parse` 失败，前端只弹一个提示，控制台无报错；
- 输入框与内部状态脱节，导致"标的已填却提示要填写"。

因此这里同时断言 **DOM 变化、提交体内容与 toast 文案**，而不只看是否抛异常。
