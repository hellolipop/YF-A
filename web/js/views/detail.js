/* ==========================================================================
   视图 · 个股详情（行情 / 盘口 / K线 / 资金流 / 信号雷达）
   ========================================================================== */
(function () {
  'use strict';

  const { h, clear, pct } = window.AD.dom;
  const F = window.AD.fmt;
  const ui = window.AD.ui;
  const api = window.AD.api;

  const PERIODS = [
    { value: 'trend', label: '分时' }, { value: '5m', label: '5分' }, { value: '15m', label: '15分' },
    { value: '30m', label: '30分' }, { value: '60m', label: '60分' },
    { value: 'day', label: '日K' }, { value: 'week', label: '周K' }, { value: 'month', label: '月K' },
  ];
  const SUBS = [{ value: 'MACD', label: 'MACD' }, { value: 'KDJ', label: 'KDJ' }, { value: 'RSI', label: 'RSI' }, { value: '', label: '关闭' }];

  function scoreRing(score) {
    const r = 38, c = 2 * Math.PI * r;
    const pctv = Math.abs(score) / 100;
    const color = score > 0 ? 'var(--up)' : (score < 0 ? 'var(--down)' : '#8b95a5');
    const svgNS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('viewBox', '0 0 92 92');
    svg.setAttribute('width', '92');
    svg.setAttribute('height', '92');
    svg.style.flex = '0 0 auto';
    const mk = (tag, attrs) => {
      const el = document.createElementNS(svgNS, tag);
      Object.keys(attrs).forEach((k) => el.setAttribute(k, attrs[k]));
      return el;
    };
    svg.appendChild(mk('circle', { cx: 46, cy: 46, r, fill: 'none', stroke: '#1b202b', 'stroke-width': 8 }));
    svg.appendChild(mk('circle', {
      cx: 46, cy: 46, r, fill: 'none', stroke: color, 'stroke-width': 8, 'stroke-linecap': 'round',
      'stroke-dasharray': (c * pctv).toFixed(2) + ' ' + c.toFixed(2),
      transform: 'rotate(-90 46 46)',
    }));
    const wrap = h('div', { class: 'score-ring' });
    wrap.appendChild(svg);
    wrap.appendChild(h('div', { class: 'val ' + (score > 0 ? 'up' : score < 0 ? 'down' : 'flat') }, [
      String(score > 0 ? '+' + score : score),
      h('small', { text: score > 0 ? '偏多信号' : score < 0 ? '偏空信号' : '中性' }),
    ]));
    return wrap;
  }

  function metric(k, v, cls, small) {
    return h('div', { class: 'metric' }, [
      h('div', { class: 'k', text: k }),
      h('div', { class: 'v ' + (cls || '') }, [String(v), small ? h('small', { text: small }) : null]),
    ]);
  }

  function mount(root, ctx) {
    const sym = ctx.state.symbol;
    if (!sym || !sym.code) {
      root.appendChild(h('div', { class: 'page' }, [
        ui.pageHead('个股详情', '从市场总览、自选股、选股器或搜索（⌘K）中选择一只标的'),
        ui.empty('尚未选择标的'),
      ]));
      return { refresh() {}, destroy() {} };
    }
    const market = sym.market;
    const code = sym.code;
    const st = {
      period: 'trend', fq: 1, showMA: true, showBOLL: false, sub: 'MACD',
      quote: null, kline: null, trends: null, orderbook: null, fundflow: null, analysis: null,
    };
    let chart = null;
    let flowChart = null;
    let timer = null;

    const headHost = h('div');
    const legendHost = h('div', { class: 'chart-legend' });
    const canvasHost = h('div');
    const obHost = h('div');
    const metricHost = h('div', { class: 'metric-list' });
    const flowHost = h('div', { style: { height: '200px' } });
    const signalHost = h('div');
    const indHost = h('div', { class: 'metric-list' });
    const metaHost = h('span', { class: 'hint' });

    /* ---------------------------------------------------------- 头部 */

    function renderHead() {
      const q = st.quote || {};
      const d = F.dir(q.changePct);
      clear(headHost);
      const stat = (k, v, cls) => h('div', { class: 'qh-stat' }, [
        h('div', { class: 'k', text: k }),
        h('div', { class: 'v ' + (cls || ''), text: v }),
      ]);
      headHost.appendChild(h('div', { class: 'quote-head' }, [
        h('div', { class: 'qh-id' }, [
          h('div', { class: 't1' }, [
            h('h1', { text: q.name || code }),
            ui.cells.star(ctx.isWatched(market, code), () => { ctx.toggleWatch(market, code, q.name || code); renderHead(); }),
          ]),
          h('div', { style: { marginTop: '3px', display: 'flex', gap: '7px', alignItems: 'center' } }, [
            h('span', { class: 'code', text: (market === 'us' ? 'US:' : '') + code }),
            h('span', { class: 'chip', text: market === 'us' ? '美股' : (code[0] === '6' ? '沪市' : code[0] === '3' ? '创业板' : code[0] === '8' || code[0] === '4' ? '北交所' : '深市') }),
            q.source ? h('span', { class: 'chip', text: q.source }) : null,
          ]),
        ]),
        h('div', { class: 'qh-px' }, [
          h('div', { class: 'big ' + d, text: F.price(q.price, market) }),
          h('div', { class: 'chg ' + d }, [
            h('span', { text: F.signed(q.change, 2) }),
            h('span', { text: F.pct(q.changePct) }),
          ]),
        ]),
        h('div', { class: 'qh-stats' }, [
          stat('今开', F.price(q.open, market), F.dir((q.open || 0) - (q.prevClose || 0))),
          stat('最高', F.price(q.high, market), ''),
          stat('最低', F.price(q.low, market), ''),
          stat('昨收', F.price(q.prevClose, market), 'dim'),
          stat('成交量', F.vol(q.volume, market)),
          stat('成交额', F.amt(q.amount, market)),
          stat('换手率', F.num(q.turnover, 2) + '%'),
          stat('量比', F.num(q.volumeRatio, 2)),
          stat('振幅', F.num(q.amplitude, 2) + '%'),
          stat('总市值', F.cap(q.marketCap, market)),
          stat('PE(TTM)', F.num(q.peTtm, 1)),
          stat('PB', F.num(q.pb, 2)),
        ]),
        h('div', { class: 'qh-actions' }, [
          h('button', { class: 'btn sm', text: '加自选', on: { click: () => { ctx.toggleWatch(market, code, q.name || code); renderHead(); } } }),
          h('button', { class: 'btn sm', text: '设预警', on: { click: () => ctx.openAlertFor(market, code, q.name || code) } }),
          h('button', { class: 'btn sm', text: '回测', on: { click: () => ctx.openBacktest(market, code, q.name || code) } }),
          h('button', { class: 'btn sm', text: '持续跟踪', on: { click: () => ctx.openTracker(market, code, q.name || code) } }),
        ]),
      ]));
      const upd = h('div', { class: 'legend-inline', style: { marginTop: '8px' } }, [
        '行情时间 ' + (q.updated || '—') + (q.stale ? ' · 数据可能延迟' : ''),
        q.week52High ? '52周最高 ' + F.price(q.week52High, market) : '',
        q.week52Low ? '52周最低 ' + F.price(q.week52Low, market) : '',
        q.avgPrice ? '均价 ' + F.price(q.avgPrice, market) : '',
      ]);
      headHost.appendChild(upd);
    }

    /* ---------------------------------------------------------- 盘口 */

    function renderOrderbook() {
      const ob = st.orderbook;
      clear(obHost);
      if (!ob || !ob.supported) {
        obHost.appendChild(h('div', { class: 'legend-inline' }, [
          (ob && ob.reason) || '五档盘口数据暂不可用',
        ]));
        if (st.quote) {
          obHost.appendChild(h('div', { style: { marginTop: '10px' } }, [
            h('div', { class: 'ob-mid' }, [
              h('span', { text: '外盘 ' + F.vol(st.quote.outer, market) }),
              h('span', { text: '内盘 ' + F.vol(st.quote.inner, market) }),
            ]),
          ]));
        }
        return;
      }
      const grid = h('div', { class: 'ob' });
      const maxVol = Math.max.apply(null, ob.asks.concat(ob.bids).map((x) => x.volume || 0).concat([1]));
      ob.asks.slice().reverse().forEach((a, i) => {
        const lvl = 5 - i;
        const row = h('div', { class: 'ob-row' }, [
          h('span', { class: 'lvl', text: '卖' + lvl }),
          h('span', { class: 'px down', text: F.price(a.price, market) }),
          h('span', { class: 'vol', text: F.vol(a.volume, market) }),
          h('div', { class: 'fill', style: { width: ((a.volume || 0) / maxVol * 100).toFixed(1) + '%', background: 'var(--down)' } }),
        ]);
        grid.appendChild(row);
      });
      grid.appendChild(h('div', { class: 'ob-sep' }));
      const q = st.quote || {};
      grid.appendChild(h('div', { class: 'ob-mid' }, [
        h('span', { class: F.dir(q.changePct), text: F.price(q.price, market) + '  ' + F.pct(q.changePct) }),
        h('span', { class: 'dim3', text: '均价 ' + F.price(ob.avgPrice || q.avgPrice, market) }),
      ]));
      grid.appendChild(h('div', { class: 'ob-sep' }));
      ob.bids.forEach((b, i) => {
        const row = h('div', { class: 'ob-row' }, [
          h('span', { class: 'lvl', text: '买' + (i + 1) }),
          h('span', { class: 'px up', text: F.price(b.price, market) }),
          h('span', { class: 'vol', text: F.vol(b.volume, market) }),
          h('div', { class: 'fill', style: { width: ((b.volume || 0) / maxVol * 100).toFixed(1) + '%', background: 'var(--up)' } }),
        ]);
        grid.appendChild(row);
      });
      obHost.appendChild(grid);
      obHost.appendChild(h('div', { class: 'legend-inline', style: { marginTop: '10px' } }, [
        '委比参考：外盘 ' + F.vol(ob.outer, market) + ' / 内盘 ' + F.vol(ob.inner, market),
      ]));
    }

    /* ------------------------------------------------------- 关键指标 */

    function renderMetrics() {
      const q = st.quote || {};
      clear(metricHost);
      const items = [
        ['今开', F.price(q.open, market)], ['昨收', F.price(q.prevClose, market)],
        ['涨停价', F.price(q.limitUp, market)], ['跌停价', F.price(q.limitDown, market)],
        ['成交量', F.vol(q.volume, market)], ['成交额', F.amt(q.amount, market)],
        ['换手率', F.num(q.turnover, 2) + '%'], ['量比', F.num(q.volumeRatio, 2)],
        ['振幅', F.num(q.amplitude, 2) + '%'], ['总市值', F.cap(q.marketCap, market)],
        ['流通市值', F.cap(q.floatCap, market)], ['PE(动)', F.num(q.pe, 1)],
        ['PE(TTM)', F.num(q.peTtm, 1)], ['市净率', F.num(q.pb, 2)],
        ['均价', F.price(q.avgPrice, market)], ['货币', q.currency || '—'],
      ];
      items.forEach(([k, v]) => metricHost.appendChild(metric(k, v)));
    }

    /* --------------------------------------------------------- 图表 */

    function setPeriod(p) {
      st.period = p;
      Array.prototype.forEach.call(periodSeg.children, (b, i) => b.classList.toggle('active', PERIODS[i].value === p));
      fqSeg.style.display = (p === 'trend') ? 'none' : 'inline-flex';
      loadChart();
    }

    async function loadChart() {
      clear(canvasHost);
      legendHost.innerHTML = '';
      if (chart) { chart.destroy(); chart = null; }
      canvasHost.appendChild(ui.loading('图表加载中…'));
      try {
        if (st.period === 'trend') {
          const res = await api.trends(market, code);
          clear(canvasHost);
          if (!res.points || !res.points.length) {
            canvasHost.appendChild(ui.empty('暂无分时数据：' + (res.error || '数据源暂不可用')));
          } else {
            chart = window.AD.chart.trend(canvasHost, {
              height: 340, prevClose: res.prevClose || (st.quote && st.quote.prevClose), market,
              onLegend: (rows) => {
                legendHost.innerHTML = '';
                rows.forEach((r) => legendHost.appendChild(h('i', { style: { color: r.color }, text: r.text })));
                legendHost.appendChild(h('span', { class: 'dim3', style: { marginLeft: 'auto' }, text: '数据源：' + (res.source || '—') }));
              },
            });
            chart.setData(res.points, {});
            metaHost.textContent = '分时 · ' + (res.source || '') + ' · 昨收 ' + F.price(res.prevClose || (st.quote && st.quote.prevClose) || 0, market);
          }
          loadSignals();
        } else {
          const res = await api.kline(market, code, st.period, st.fq, 320);
          clear(canvasHost);
          if (!res.bars || !res.bars.length) {
            canvasHost.appendChild(ui.empty('暂无K线数据：' + (res.error || '数据源暂不可用')));
            return;
          }
          st.kline = res;
          chart = window.AD.chart.kline(canvasHost, {
            height: 430, period: st.period, market, showMA: st.showMA, showBOLL: st.showBOLL, sub: st.sub,
            onLegend: (rows) => {
              legendHost.innerHTML = '';
              rows.forEach((r) => legendHost.appendChild(h('i', { style: { color: r.color }, text: r.text })));
              legendHost.appendChild(h('span', { class: 'dim3', style: { marginLeft: 'auto' }, text: '数据源：' + (res.source || '—') }));
            },
          });
          chart.setData(res.bars, {});
          metaHost.textContent = res.bars.length + ' 根K线 · ' + (res.source || '') + ' · 复权方式 ' +
            (['不复权', '前复权', '后复权'][st.fq] || '—');
          if (st.period !== 'day') loadSignals();
          else runAnalysis(res.bars);
        }
      } catch (e) {
        clear(canvasHost);
        canvasHost.appendChild(ui.empty('图表加载失败：' + e.message));
      }
    }

    let signalsLoaded = false;
    async function loadSignals() {
      if (signalsLoaded) return;
      signalsLoaded = true;
      try {
        const res = await api.kline(market, code, 'day', 1, 320);
        if (res.bars && res.bars.length >= 30) runAnalysis(res.bars);
        else signalHost.appendChild(ui.empty('日线数据不足，暂无法生成信号雷达'));
      } catch (e) {
        signalHost.appendChild(ui.empty('信号雷达数据获取失败：' + e.message));
      }
    }

    /* ------------------------------------------------------- 信号雷达 */

    function runAnalysis(bars) {
      const res = window.AD.quant.analyze(bars);
      st.analysis = res;
      clear(signalHost);
      clear(indHost);
      if (!res.signals.length) {
        signalHost.appendChild(ui.empty('当前没有明显技术信号'));
      } else {
        const list = h('div', { class: 'signal-list' });
        res.signals.slice().sort((a, b) => Math.abs(b.weight * b.dir) - Math.abs(a.weight * a.dir)).forEach((s) => {
          list.appendChild(h('div', { class: 'signal-item' }, [
            h('span', { class: 'dotm', style: { background: s.dir > 0 ? 'var(--up)' : 'var(--down)' } }),
            h('span', { class: 'txt' }, [h('b', { text: s.name }), '　' + s.desc]),
            h('span', { class: 'w', text: (s.dir > 0 ? '+' : '-') + s.weight }),
          ]));
        });
        signalHost.appendChild(list);
      }
      const i = res.ind || {};
      const metrics = [
        ['均线 MA5 / MA20', F.num(i.ma5, 2) + ' / ' + F.num(i.ma20, 2)],
        ['MA60', F.num(i.ma60, 2)],
        ['MACD DIF / DEA', F.num(i.dif, 3) + ' / ' + F.num(i.dea, 3)],
        ['RSI6 / RSI14', F.num(i.rsi6, 1) + ' / ' + F.num(i.rsi14, 1)],
        ['KDJ K / D / J', F.num(i.k, 1) + ' / ' + F.num(i.d, 1) + ' / ' + F.num(i.j, 1)],
        ['布林上轨 / 下轨', F.num(i.bollUp, 2) + ' / ' + F.num(i.bollLow, 2)],
        ['ATR / 波动率', F.num(i.atr, 2) + ' / ' + F.num(i.atrPct, 2) + '%'],
        ['20日高 / 低', F.num(i.hi20, 2) + ' / ' + F.num(i.lo20, 2)],
        ['60日高 / 低', F.num(i.hi60, 2) + ' / ' + F.num(i.lo60, 2)],
      ];
      metrics.forEach(([k, v]) => indHost.appendChild(metric(k, v)));
      signalHost.insertBefore(h('div', { class: 'score-wrap', style: { marginBottom: '12px' } }, [
        scoreRing(res.score),
        h('div', {}, [
          h('div', { style: { fontSize: '13px' }, text: '综合技术面：' + res.bias }),
          h('div', { class: 'legend-inline', style: { marginTop: '6px' } }, [
            '基于 ' + res.signals.length + ' 项信号加权（趋势 / 动能 / 超买超卖 / 量能 / 位置）',
          ]),
          h('div', { class: 'legend-inline', style: { marginTop: '4px', color: 'var(--text-3)' } }, [
            '信号仅反映历史价量统计特征，不构成买卖建议',
          ]),
        ]),
      ]), signalHost.firstChild);
    }

    /* ------------------------------------------------------- 资金流 */

    async function loadFlow() {
      clear(flowHost);
      flowHost.appendChild(ui.loading('资金流加载中…'));
      try {
        const res = await api.fundflow(market, code);
        clear(flowHost);
        if (!res.series || !res.series.length) {
          flowHost.appendChild(ui.empty('资金流数据暂不可用' + (res.error ? '：' + res.error : '')));
          return;
        }
        st.fundflow = res;
        const data = res.series.map((x) => ({ t: x.t, v: x.main }));
        flowChart = window.AD.chart.line(flowHost, {
          height: 200, zeroLine: true, fmt: (v) => F.amt(v, market),
          series: [{
            name: '主力净额', data, color: res.series[res.series.length - 1].main >= 0 ? 'var(--up)' : 'var(--down)',
            fill: 'rgba(77,141,255,0.10)', width: 1.4,
            fmt: (v) => F.amt(v, market),
          }],
        });
        const last = res.series[res.series.length - 1];
        const note = h('div', { class: 'legend-inline', style: { marginTop: '8px' } }, [
          '最新（' + last.t + '）主力净额 ' + F.amt(last.main, market),
          res.limited ? '数据源：' + res.source : '含超大单 / 大单 / 中单 / 小单拆分',
        ]);
        flowHost.appendChild(note);
        if (!res.limited) {
          const detail = h('div', { class: 'metric-list', style: { marginTop: '10px' } });
          [['超大单', last.huge], ['大单', last.big], ['中单', last.mid], ['小单', last.small]].forEach(([k, v]) => {
            detail.appendChild(metric(k + '净额', F.amt(v, market), F.dir(v)));
          });
          flowHost.appendChild(detail);
        }
      } catch (e) {
        clear(flowHost);
        flowHost.appendChild(ui.empty('资金流加载失败：' + e.message));
      }
    }

    /* --------------------------------------------------------- 工具栏 */

    const periodSeg = h('div', { class: 'seg' });
    PERIODS.forEach((p) => {
      periodSeg.appendChild(h('button', {
        class: st.period === p.value ? 'active' : '', text: p.label,
        on: { click: () => setPeriod(p.value) },
      }));
    });

    const fqSeg = h('div', { class: 'seg' }, [
      h('button', { class: 'active', text: '前复权', on: { click: (e) => { st.fq = 1; markSeg(fqSeg, e.target); loadChart(); } } }),
      h('button', { text: '不复权', on: { click: (e) => { st.fq = 0; markSeg(fqSeg, e.target); loadChart(); } } }),
      h('button', { text: '后复权', on: { click: (e) => { st.fq = 2; markSeg(fqSeg, e.target); loadChart(); } } }),
    ]);

    const subSeg = h('div', { class: 'seg' });
    SUBS.forEach((s) => {
      subSeg.appendChild(h('button', {
        class: st.sub === s.value ? 'active' : '', text: s.label,
        on: {
          click: (e) => {
            st.sub = s.value;
            markSeg(subSeg, e.target);
            if (chart && st.period !== 'trend') { chart.setSub(s.value); }
          },
        },
      }));
    });

    function markSeg(seg, btn) {
      Array.prototype.forEach.call(seg.children, (b) => b.classList.toggle('active', b === btn));
    }

    const maToggle = h('button', {
      class: 'btn sm active', text: '均线',
      on: {
        click: (e) => {
          st.showMA = !st.showMA;
          e.target.classList.toggle('active', st.showMA);
          if (chart && st.period !== 'trend') chart.setMA(st.showMA);
        },
      },
    });
    const bollToggle = h('button', {
      class: 'btn sm', text: 'BOLL',
      on: {
        click: (e) => {
          st.showBOLL = !st.showBOLL;
          e.target.classList.toggle('active', st.showBOLL);
          if (chart && st.period !== 'trend') chart.setBOLL(st.showBOLL);
        },
      },
    });

    root.appendChild(h('div', { class: 'page' }, [
      ui.pageHead('个股详情', '实时行情 · 五档盘口 · 多周期K线 · 技术信号雷达 · 资金流', [
        metaHost,
        h('button', {
          class: 'btn sm', text: '刷新',
          on: { click: () => { loadQuote(); loadChart(); loadFlow(); } },
        }),
      ]),
      headHost,
      h('div', { style: { height: '16px' } }),
      h('div', { class: 'grid g-2-1' }, [
        h('div', {}, [
          ui.section('价格走势', '滚轮缩放 · 拖拽平移 · 双击复位', [], h('div', { class: 'chart-panel' }, [
            h('div', { class: 'chart-toolbar' }, [periodSeg, fqSeg, h('span', { class: 'spacer' }), maToggle, bollToggle, h('span', { class: 'dim3', text: '副图' }), subSeg]),
            legendHost,
            canvasHost,
          ])),
          ui.section('技术信号雷达', '多指标加权评分', [], signalHost),
          ui.section('资金流向', '近 60 个交易日主力资金净额', [], flowHost),
        ]),
        h('div', {}, [
          ui.section('五档盘口', market === 'us' ? '美股提供最优买卖一档' : '买卖五档实时挂单', [], obHost),
          ui.section('关键指标', '', [], metricHost),
          ui.section('技术指标读数', '基于当前周期K线实时计算', [], indHost),
        ]),
      ]),
    ]));

    async function loadQuote() {
      try {
        const q = await api.stock(market, code);
        st.quote = q;
        renderHead();
        renderMetrics();
        renderOrderbook();
      } catch (e) {
        ctx.toast('行情获取失败：' + e.message, 'err');
      }
    }

    (async () => {
      await loadQuote();
      await loadChart();
      loadFlow();
      try {
        const ob = await api.orderbook(market, code);
        st.orderbook = ob;
        renderOrderbook();
      } catch (e) { /* 忽略盘口失败 */ }
    })();

    timer = setInterval(() => {
      loadQuote().then(() => {
        if (st.period === 'trend') loadChart();
      });
    }, Math.max(6000, ctx.state.pollMs));

    return {
      refresh: () => { loadQuote(); loadChart(); loadFlow(); },
      destroy() {
        if (timer) clearInterval(timer);
        if (chart) chart.destroy();
        if (flowChart) flowChart.destroy();
      },
    };
  }

  window.AD = window.AD || {};
  window.AD.views = window.AD.views || {};
  window.AD.views.detail = { mount };
})();
