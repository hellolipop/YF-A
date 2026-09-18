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
node tests/ui/seamless_refresh.js

# 若 jsdom 装在别处
NODE_PATH=/path/to/node_modules node tests/ui/create_tracker_sync.js
NODE_PATH=/path/to/node_modules node tests/ui/seamless_refresh.js

# 服务不在默认端口
AD_BASE=http://127.0.0.1:9000 node tests/ui/create_tracker_sync.js
```

缺少 jsdom 或服务未启动时会打印原因并直接跳过，不会误报失败。

## 用例

| 文件 | 覆盖内容 |
| --- | --- |
| `create_tracker_sync.js` | 新建跟踪任务的表单状态同步：输入框有值但不触发 blur、创建后表单残留、改代码后沿用旧名称、成本假设是否保留 |
| `seamless_refresh.js` | **无感刷新（更新不重构图）+ 美股数据源**：A 段离线断言增量更新语义（数据未变时零 DOM 写入、顺序变化只移动节点、焦点输入框不被覆盖、canvas 不被替换、行内监听读到最新数据）；B 段在真实页面上断言自选股轮询后表体/首行、个股详情刷新与切换周期后的 canvas 都是**同一个节点对象**，且刷新期间不出现「加载中」占位；C 段断言「美股数据源」选项（币安 bStocks · 7×24）：控件只在美股出现、切源后页头口径与 24h 标签、口径说明、五档盘口、资金流/AI 区块的如实标注，以及切回后口径标注全部撤回 |

## 为什么需要它

后端单元测试（`tests/test_*.py`）与接口审计（`tests/audit_json_endpoints.py`）
覆盖不到浏览器侧的表单与渲染行为。此前几个真实故障都属于这一类：

- 服务端返回 `Infinity` 导致浏览器 `JSON.parse` 失败，前端只弹一个提示，控制台无报错；
- 输入框与内部状态脱节，导致"标的已填却提示要填写"。

因此这里同时断言 **DOM 变化、提交体内容与 toast 文案**，而不只看是否抛异常。

`seamless_refresh.js` 用 `MutationObserver` 与节点身份比较来锁住"更新即重构图"的回归：
只看"渲染有没有报错"是不够的，整块 `clear() + 重建` 同样不报错，但界面会闪、
滚动位置与 hover 会丢——这正是用户实际反馈的那类问题。
