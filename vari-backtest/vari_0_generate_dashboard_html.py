#!/usr/bin/env python3
"""
Build a dashboard HTML file from baseline_dashboard.json (or compatible shape).

Visual layout matches backtest-strategies/output_sample_mean_revert_backtest.html
(Chart.js 4.4.1, same CSS, same chart/table behavior).
"""
from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent

# Placeholders are replaced after load — JSON must not pass through str.format.
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__HTML_TITLE__</title>
<script>
(function(){var k='vari_dashboard_theme';var t=localStorage.getItem(k)||'dark';if(t!=='dark'&&t!=='light')t='dark';document.documentElement.setAttribute('data-theme',t);})();
</script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root[data-theme="dark"]{
  --bg:#0e1014;
  --surface:#181c24;
  --surface-2:#222831;
  --text:#e8eaed;
  --text-sub:#9aa0a6;
  --border:rgba(255,255,255,0.1);
  --grid:rgba(255,255,255,0.08);
  --muted:#8b919a;
  --reason-text:#b8bcc4;
  --tab-border:rgba(255,255,255,0.15);
  --pill-long-bg:#153d30;
  --pill-long-fg:#6ee7b7;
  --pill-short-bg:#3d1a1f;
  --pill-short-fg:#fca5a5;
  --pill-trade-bg:#1a2f4a;
  --pill-trade-fg:#93c5fd;
  --dot-skip:#5c6370;
}
:root[data-theme="light"]{
  --bg:#fff;
  --surface:#f7f7f5;
  --surface-2:#fff;
  --text:#1a1a1a;
  --text-sub:#888;
  --border:rgba(0,0,0,0.08);
  --grid:rgba(0,0,0,0.06);
  --muted:#888;
  --reason-text:#444;
  --tab-border:#ccc;
  --pill-long-bg:#E1F5EE;
  --pill-long-fg:#0F6E56;
  --pill-short-bg:#FCEBEB;
  --pill-short-fg:#A32D2D;
  --pill-trade-bg:#E6F1FB;
  --pill-trade-fg:#185FA5;
  --dot-skip:#ccc;
}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);padding:2rem;min-height:100vh;transition:background .2s ease,color .2s ease}
.theme-bar{display:flex;justify-content:flex-end;margin-bottom:10px}
.theme-toggle{font:inherit;font-size:12px;padding:6px 14px;border-radius:8px;cursor:pointer;border:1px solid var(--tab-border);background:var(--surface);color:var(--text-sub)}
.theme-toggle:hover{color:var(--text);border-color:var(--muted)}
.title{font-size:18px;font-weight:500;margin-bottom:4px;color:var(--text)}
.subtitle{font-size:13px;color:var(--text-sub);margin-bottom:1.5rem}
.metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-bottom:1.5rem}
.metric{background:var(--surface);border-radius:8px;padding:12px 14px;border:1px solid var(--border)}
.metric-label{font-size:12px;color:var(--text-sub);margin-bottom:4px}
.metric-value{font-size:22px;font-weight:500}
.metric-value.pos{color:#3ecf8e}
.metric-value.neg{color:#f87171}
.metric-value.neu{color:var(--text)}
.section{margin-bottom:1.5rem}
.section-title{font-size:11px;font-weight:500;margin-bottom:10px;color:var(--text-sub);text-transform:uppercase;letter-spacing:.04em}
.chart-wrap{position:relative;width:100%}
.tabs{display:flex;gap:6px;margin-bottom:12px}
.tab{padding:5px 12px;border:1px solid var(--tab-border);border-radius:8px;font-size:12px;cursor:pointer;background:transparent;color:var(--text-sub)}
.tab.active{background:var(--surface-2);color:var(--text);font-weight:500;border-color:var(--muted)}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:6px 10px;border-bottom:1px solid var(--border);color:var(--text-sub);font-weight:400;font-size:11px}
td{padding:7px 10px;border-bottom:1px solid var(--border);color:var(--text)}
.row-click{cursor:pointer}
.row-click:hover{background:rgba(255,255,255,0.03)}
.details-cell{padding:0;border-bottom:1px solid var(--border)}
.details-inner{padding:10px 10px 12px 10px}
.subtable{width:100%;border-collapse:collapse;font-size:13px}
.subtable th{font-size:11px;color:var(--text-sub);font-weight:400;border-bottom:1px solid var(--border);padding:6px 10px}
.subtable td{border-bottom:1px solid var(--border);padding:7px 10px}
.subtable th.sortable{cursor:pointer;user-select:none}
.subtable th.sortable:hover{color:var(--text)}
.sort-ind{display:inline-block;width:12px;margin-left:4px;color:var(--muted)}
.table-sort th.sortable{cursor:pointer;user-select:none}
.table-sort th.sortable:hover{color:var(--text)}
.caret{display:inline-block;width:14px;color:var(--muted);font-size:28px;line-height:1}
.flash-row{animation:flashRow 0.9s ease-in-out 0s 1}
@keyframes flashRow{
  0%{background:rgba(147,197,253,0.00)}
  20%{background:rgba(147,197,253,0.18)}
  100%{background:rgba(147,197,253,0.00)}
}
.pill{display:inline-block;padding:2px 7px;border-radius:10px;font-size:11px;font-weight:500}
.pill.long{background:var(--pill-long-bg);color:var(--pill-long-fg)}
.pill.short{background:var(--pill-short-bg);color:var(--pill-short-fg)}
.pill.trade{background:var(--pill-trade-bg);color:var(--pill-trade-fg)}
.legend{display:flex;gap:16px;font-size:12px;color:var(--text-sub);margin-bottom:8px}
.legend span{display:flex;align-items:center;gap:5px}
.dot{width:10px;height:10px;border-radius:2px;flex-shrink:0}
.note{font-size:12px;color:var(--text-sub);margin-top:8px}
.td-muted{color:var(--muted)}
</style>
</head>
<body>

<div class="theme-bar">
  <button type="button" class="theme-toggle" id="themeToggle" aria-label="Toggle color theme">Light mode</button>
</div>
<div class="title">__TITLE__</div>
<div class="subtitle">__SUBTITLE__</div>

<div class="metrics">
  <div class="metric"><div class="metric-label">Return on notional</div><div class="metric-value __CLS_RON__">__RETURN_ON_NOTIONAL__</div></div>
  <div class="metric"><div class="metric-label">Total PnL</div><div class="metric-value __CLS_TOTAL__">__TOTAL_PNL__</div></div>
  <div class="metric"><div class="metric-label">Win rate</div><div class="metric-value neu">__WIN_RATE__</div></div>
  <div class="metric"><div class="metric-label">Wins / losses</div><div class="metric-value neu">__WINS_LOSSES__</div></div>
  <div class="metric"><div class="metric-label">Active days</div><div class="metric-value neu">__ACTIVE_DAYS__</div></div>
  <div class="metric"><div class="metric-label">Sharpe Ratio</div><div class="metric-value neu">__SHARPE__</div></div>
  <div class="metric"><div class="metric-label">Avg daily PnL</div><div class="metric-value __CLS_AVG__">__AVG_DAILY__</div></div>
  <div class="metric"><div class="metric-label">Max drawdown</div><div class="metric-value neg">__MAX_DD__</div></div>
  <div class="metric"><div class="metric-label">Worst day</div><div class="metric-value neg">__WORST_DAY__</div></div>
  <div class="metric"><div class="metric-label">Best day</div><div class="metric-value pos">__BEST_DAY__</div></div>
</div>

<div class="section">
  <div class="section-title">Equity curve</div>
  <div class="legend">
    <span><span class="dot" style="background:#1D9E75"></span>Cumulative PnL</span>
    <span><span class="dot" style="background:var(--dot-skip)"></span>BTC-filtered / skipped day</span>
  </div>
  <div class="chart-wrap" style="height:220px"><canvas id="equityChart"></canvas></div>
</div>

<div class="section">
  <div class="section-title">Daily PnL bars</div>
  <div class="chart-wrap" style="height:180px"><canvas id="dailyChart"></canvas></div>
</div>

__DAILY_SESSION_SECTION__

<div class="section">
  <div class="section-title">Monthly breakdown</div>
  <div class="chart-wrap" style="height:130px"><canvas id="monthlyChart"></canvas></div>
</div>

<div class="section">
  <div class="section-title">Top and Bottom Tickers</div>
  <div class="tabs">
    <button class="tab active" onclick="showTickerTab('top_pnl',this)">Top 10 PNL</button>
    <button class="tab" onclick="showTickerTab('btm_pnl',this)">Btm 10 PNL</button>
    <button class="tab" onclick="showTickerTab('top_wr',this)">Top 10 WR</button>
    <button class="tab" onclick="showTickerTab('btm_wr',this)">Btm 10 WR</button>
    <button class="tab" onclick="showTickerTab('top_traded',this)">Top 10 Traded</button>
  </div>
  <div id="tick-top-pnl" class="table-wrap">
    <table>
      <thead><tr class="table-sort"><th>#</th><th class="sortable" data-key="ticker">Ticker<span class="sort-ind"></span></th><th class="sortable" data-key="trades">Trades<span class="sort-ind"></span></th><th class="sortable" data-key="pnl">Cum PnL<span class="sort-ind"></span></th></tr></thead>
      <tbody id="top-body"></tbody>
    </table>
  </div>
  <div id="tick-btm-pnl" class="table-wrap" style="display:none">
    <table>
      <thead><tr class="table-sort"><th>#</th><th class="sortable" data-key="ticker">Ticker<span class="sort-ind"></span></th><th class="sortable" data-key="trades">Trades<span class="sort-ind"></span></th><th class="sortable" data-key="pnl">Cum PnL<span class="sort-ind"></span></th></tr></thead>
      <tbody id="worst-body"></tbody>
    </table>
  </div>
  <div id="tick-top-wr" class="table-wrap" style="display:none">
    <table>
      <thead><tr class="table-sort"><th>#</th><th class="sortable" data-key="ticker">Ticker<span class="sort-ind"></span></th><th class="sortable" data-key="trades">No. of Trades<span class="sort-ind"></span></th><th class="sortable" data-key="winrate">Win Rate<span class="sort-ind"></span></th><th class="sortable" data-key="pnl">Cum PnL<span class="sort-ind"></span></th></tr></thead>
      <tbody id="topwr-body"></tbody>
    </table>
  </div>
  <div id="tick-btm-wr" class="table-wrap" style="display:none">
    <table>
      <thead><tr class="table-sort"><th>#</th><th class="sortable" data-key="ticker">Ticker<span class="sort-ind"></span></th><th class="sortable" data-key="trades">No. of Trades<span class="sort-ind"></span></th><th class="sortable" data-key="winrate">Win Rate<span class="sort-ind"></span></th><th class="sortable" data-key="pnl">Cum PnL<span class="sort-ind"></span></th></tr></thead>
      <tbody id="btmwr-body"></tbody>
    </table>
  </div>
  <div id="tick-top-traded" class="table-wrap" style="display:none">
    <table>
      <thead><tr class="table-sort"><th>#</th><th class="sortable" data-key="ticker">Ticker<span class="sort-ind"></span></th><th class="sortable" data-key="trades">No. of Trades<span class="sort-ind"></span></th><th class="sortable" data-key="winrate">Win Rate<span class="sort-ind"></span></th><th class="sortable" data-key="pnl">Cum PnL<span class="sort-ind"></span></th><th class="sortable" data-key="avgpnl">Avg PnL<span class="sort-ind"></span></th></tr></thead>
      <tbody id="toptraded-body"></tbody>
    </table>
  </div>
</div>

<div class="section">
  <div class="section-title">Trade log — largest trades by PnL impact</div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Date</th><th>Ticker</th><th>Side</th><th>Entry</th><th>Exit</th><th>Ret%</th><th>PnL</th></tr></thead>
      <tbody id="trade-body"></tbody>
    </table>
  </div>
  <div class="note">Showing top 20 trades by absolute PnL. Equal long/short count each day (no fixed pair cap); equal notional per leg.</div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
const DATA = __DATA_JSON__;
const THEME_KEY='vari_dashboard_theme';

function fmtPnl(v){const a=Math.abs(v).toFixed(2);if(v>0)return'+$'+a;if(v<0)return'-$'+a;return'$'+a;}
function fmtPct(v){return (v>=0?'+':'')+v.toFixed(2)+'%'}

function currentTheme(){
  var t=document.documentElement.getAttribute('data-theme');
  return t==='light'?'light':'dark';
}
function chartPalette(){
  var d=currentTheme()==='dark';
  return{
    tick:d?'#9aa0a6':'#666',
    grid:d?'rgba(255,255,255,0.08)':'rgba(0,0,0,0.06)',
    skipSeg:d?'#6b7280':'#B4B2A9',
    eqFill:d?'rgba(29,158,117,0.14)':'rgba(29,158,117,0.08)'
  };
}
function syncThemeButton(){
  var b=document.getElementById('themeToggle');
  if(!b)return;
  var dark=currentTheme()==='dark';
  b.textContent=dark?'Light mode':'Dark mode';
}
function applyChartTheme(){
  var p=chartPalette();
  Chart.defaults.color=p.tick;
  function patch(ch){
    if(!ch||!ch.options||!ch.options.scales)return;
    var s=ch.options.scales;
    ['x','y'].forEach(function(ax){
      var sc=s[ax];
      if(!sc)return;
      if(sc.ticks)sc.ticks.color=p.tick;
      if(sc.grid&&sc.grid.display!==false)sc.grid.color=p.grid;
    });
    ch.update('none');
  }
  patch(window.__eqChart);
  patch(window.__dayChart);
  patch(window.__monChart);
  if(window.__eqChart&&window.__eqChart.data&&window.__eqChart.data.datasets[0]){
    window.__eqChart.data.datasets[0].backgroundColor=chartPalette().eqFill;
    window.__eqChart.update('none');
  }
}

document.getElementById('themeToggle').addEventListener('click',function(){
  var next=currentTheme()==='dark'?'light':'dark';
  document.documentElement.setAttribute('data-theme',next);
  localStorage.setItem(THEME_KEY,next);
  syncThemeButton();
  applyChartTheme();
});

Chart.defaults.color=chartPalette().tick;

window.__eqChart=new Chart(document.getElementById('equityChart'),{
  type:'line',
  data:{
    labels:DATA.equity.map(function(d){return d.date.slice(5);}),
    datasets:[{
      label:'Equity',
      data:DATA.equity.map(function(d){return d.equity;}),
      borderColor:'#1D9E75',
      borderWidth:2,
      pointRadius:DATA.equity.map(function(d){return d.skipped?0:2;}),
      pointBackgroundColor:'#1D9E75',
      fill:true,
      backgroundColor:chartPalette().eqFill,
      tension:0.3,
      segment:{
        borderColor:function(ctx){
          var sk=DATA.equity[ctx.p0DataIndex]&&DATA.equity[ctx.p0DataIndex].skipped;
          return sk?chartPalette().skipSeg:'#1D9E75';
        },
        borderDash:function(ctx){
          return DATA.equity[ctx.p0DataIndex]&&DATA.equity[ctx.p0DataIndex].skipped?[4,4]:[];
        }
      }
    }]
  },
  options:{
    responsive:true,maintainAspectRatio:false,
    plugins:{legend:{display:false},tooltip:{callbacks:{
      label:function(ctx){
        var d=DATA.equity[ctx.dataIndex];
        return (d.skipped?'[skipped] ':'')+fmtPnl(ctx.parsed.y);
      }
    }}},
    scales:{
      x:{ticks:{maxTicksLimit:10,color:chartPalette().tick},grid:{color:chartPalette().grid}},
      y:{ticks:{color:chartPalette().tick,callback:function(v){return '$'+v.toFixed(0);}},grid:{color:chartPalette().grid}}
    }
  }
});

window.__dayChart=new Chart(document.getElementById('dailyChart'),{
  type:'bar',
  data:{
    labels:DATA.daily.map(function(d){return d.date.slice(5);}),
    datasets:[{
      data:DATA.daily.map(function(d){return d.pnl;}),
      backgroundColor:DATA.daily.map(function(d){
        return d.pnl>=0?'rgba(29,158,117,0.75)':'rgba(226,75,74,0.75)';
      }),
      borderRadius:2
    }]
  },
  options:{
    responsive:true,maintainAspectRatio:false,
    plugins:{legend:{display:false},tooltip:{callbacks:{label:function(ctx){return fmtPnl(ctx.parsed.y);}}}},
    onHover:function(evt, active){
      var el = evt && evt.native ? evt.native.target : null;
      if(el && el.style) el.style.cursor = (active && active.length) ? 'pointer' : 'default';
    },
    scales:{
      x:{ticks:{maxTicksLimit:12,color:chartPalette().tick},grid:{display:false}},
      y:{ticks:{color:chartPalette().tick,callback:function(v){return '$'+v;}},grid:{color:chartPalette().grid}}
    }
  }
});

window.__monChart=new Chart(document.getElementById('monthlyChart'),{
  type:'bar',
  data:{
    labels:Object.keys(DATA.monthly),
    datasets:[{
      data:Object.values(DATA.monthly),
      backgroundColor:Object.keys(DATA.monthly).map(function(){return 'rgba(29,158,117,0.8)';}),
      borderRadius:4
    }]
  },
  options:{
    responsive:true,maintainAspectRatio:false,
    plugins:{legend:{display:false},tooltip:{callbacks:{label:function(ctx){return fmtPnl(ctx.parsed.y);}}}},
    scales:{
      x:{ticks:{color:chartPalette().tick},grid:{display:false}},
      y:{ticks:{color:chartPalette().tick,callback:function(v){return '$'+v;}},grid:{color:chartPalette().grid}}
    }
  }
});

syncThemeButton();
applyChartTheme();

function pnlCellColor(pos){return pos?'#3ecf8e':'#f87171';}

function renderCoinTable(id, data){
  var tbody=document.getElementById(id);
  if(!tbody){return;}
  tbody.innerHTML='';
  data.forEach(function(c,i){
    var pos=c.pnl>=0;
    tbody.innerHTML+='<tr data-ticker="'+(c.coin||'')+'" data-trades="'+(c.trades||0)+'" data-pnl="'+(c.pnl||0)+'">'+
      '<td class="td-muted">'+(i+1)+'</td>'+
      '<td><span class="pill trade">'+c.coin+'</span></td>'+
      '<td>'+c.trades+'</td>'+
      '<td style="color:'+pnlCellColor(pos)+';font-weight:500">'+fmtPnl(c.pnl)+'</td>'+
      '</tr>';
  });
}
renderCoinTable('top-body', DATA.top);
renderCoinTable('worst-body', DATA.worst);

function showTickerTab(which, el){
  document.querySelectorAll('.tab').forEach(function(t){t.classList.remove('active');});
  el.classList.add('active');
  document.getElementById('tick-top-pnl').style.display=which==='top_pnl'?'block':'none';
  document.getElementById('tick-btm-pnl').style.display=which==='btm_pnl'?'block':'none';
  document.getElementById('tick-top-wr').style.display=which==='top_wr'?'block':'none';
  document.getElementById('tick-btm-wr').style.display=which==='btm_wr'?'block':'none';
  document.getElementById('tick-top-traded').style.display=which==='top_traded'?'block':'none';
}

function buildWinRateTables(){
  var tbTop=document.getElementById('topwr-body');
  var tbBtm=document.getElementById('btmwr-body');
  var tbTraded=document.getElementById('toptraded-body');
  if(!tbTop||!tbBtm||!tbTraded||!DATA||!Array.isArray(DATA.trades))return;
  var by={};
  DATA.trades.forEach(function(t){
    var k=t.coin || t.coin_id || '';
    if(!k)return;
    var o=by[k] || (by[k]={ticker:k, trades:0, wins:0, pnl:0});
    o.trades += 1;
    var p = (typeof t.pnl==='number') ? t.pnl : 0;
    o.pnl += p;
    if(p > 0) o.wins += 1;
  });
  var rows=Object.keys(by).map(function(k){
    var o=by[k];
    o.winRate = o.trades ? (o.wins / o.trades) : 0;
    return o;
  }).filter(function(o){ return o.trades > 0; });

  function renderWR(tbody, data){
    tbody.innerHTML='';
    data.forEach(function(r,i){
      var pos=r.pnl>=0;
      var wrPct = (r.winRate*100);
      var wrTxt = wrPct.toFixed(0)+'% ('+r.wins+'/'+r.trades+')';
      tbody.innerHTML += '<tr data-ticker="'+r.ticker+'" data-trades="'+r.trades+'" data-winrate="'+r.winRate+'" data-pnl="'+r.pnl+'">'+
        '<td class="td-muted">'+(i+1)+'</td>'+
        '<td><span class="pill trade">'+r.ticker+'</span></td>'+
        '<td>'+r.trades+'</td>'+
        '<td>'+wrTxt+'</td>'+
        '<td style="color:'+pnlCellColor(pos)+';font-weight:500">'+fmtPnl(r.pnl)+'</td>'+
      '</tr>';
    });
  }

  function renderTraded(tbody, data){
    tbody.innerHTML='';
    data.forEach(function(r,i){
      var pos=r.pnl>=0;
      var wrPct = (r.winRate*100);
      var wrTxt = wrPct.toFixed(0)+'% ('+r.wins+'/'+r.trades+')';
      var avg = r.trades ? (r.pnl / r.trades) : 0;
      tbody.innerHTML += '<tr data-ticker="'+r.ticker+'" data-trades="'+r.trades+'" data-winrate="'+r.winRate+'" data-pnl="'+r.pnl+'" data-avgpnl="'+avg+'">'+
        '<td class="td-muted">'+(i+1)+'</td>'+
        '<td><span class="pill trade">'+r.ticker+'</span></td>'+
        '<td>'+r.trades+'</td>'+
        '<td>'+wrTxt+'</td>'+
        '<td style="color:'+pnlCellColor(pos)+';font-weight:500">'+fmtPnl(r.pnl)+'</td>'+
        '<td style="color:'+pnlCellColor(avg>=0)+';font-weight:500">'+fmtPnl(avg)+'</td>'+
      '</tr>';
    });
  }

  var topWR = rows.slice().sort(function(a,b){
    if(b.winRate!==a.winRate) return b.winRate-a.winRate;
    return b.trades-a.trades;
  }).slice(0,10);

  var btmWR = rows.slice().sort(function(a,b){
    if(a.winRate!==b.winRate) return a.winRate-b.winRate;
    return b.trades-a.trades;
  }).slice(0,10);

  var topTraded = rows.slice().sort(function(a,b){
    if(b.trades!==a.trades) return b.trades-a.trades;
    return b.pnl-a.pnl;
  }).slice(0,10);

  renderWR(tbTop, topWR);
  renderWR(tbBtm, btmWR);
  renderTraded(tbTraded, topTraded);
}
buildWinRateTables();

function attachSortableTable(wrapperId){
  var wrap=document.getElementById(wrapperId);
  if(!wrap)return;
  var table=wrap.querySelector('table');
  if(!table)return;
  var tbody=table.querySelector('tbody');
  var ths=table.querySelectorAll('th.sortable');
  if(!tbody||!ths||!ths.length)return;

  function clearIndicators(){
    table.querySelectorAll('.sort-ind').forEach(function(x){x.textContent='';});
  }

  function getVal(tr, key){
    var v = tr.getAttribute('data-'+key);
    if(key==='ticker') return (v||'').toLowerCase();
    var n = parseFloat(v);
    return isFinite(n) ? n : 0;
  }

  function sortBy(key, dir){
    var mul = dir==='desc' ? -1 : 1;
    var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
    rows.sort(function(a,b){
      var av=getVal(a,key), bv=getVal(b,key);
      if(typeof av==='string' || typeof bv==='string'){
        return mul*(av<bv?-1:av>bv?1:0);
      }
      return mul*(av-bv);
    });
    tbody.innerHTML='';
    rows.forEach(function(r,i){
      var idxCell=r.querySelector('td.td-muted');
      if(idxCell) idxCell.textContent=String(i+1);
      tbody.appendChild(r);
    });
  }

  ths.forEach(function(th){
    th.addEventListener('click', function(){
      var key = th.getAttribute('data-key');
      var curKey = table.getAttribute('data-sort-key')||'';
      var curDir = table.getAttribute('data-sort-dir')||'asc';
      var nextDir = (curKey===key && curDir==='asc') ? 'desc' : 'asc';
      table.setAttribute('data-sort-key', key);
      table.setAttribute('data-sort-dir', nextDir);
      clearIndicators();
      var ind = th.querySelector('.sort-ind');
      if(ind) ind.textContent = nextDir==='asc' ? '▲' : '▼';
      sortBy(key, nextDir);
    });
  });
}

attachSortableTable('tick-top-pnl');
attachSortableTable('tick-btm-pnl');
attachSortableTable('tick-top-wr');
attachSortableTable('tick-btm-wr');
attachSortableTable('tick-top-traded');

var FLASH_REMOVE_MS = 1000;
var SCROLL_SETTLE_MS = 120;
var SCROLL_MAX_WAIT_MS = 2000;

function afterScrollSettles(targetEl, fn){
  var start = performance.now ? performance.now() : Date.now();
  var stableSince = null;
  var lastY = window.scrollY || 0;
  var lastTop = null;

  function tick(){
    var now = performance.now ? performance.now() : Date.now();
    if(!targetEl || !targetEl.getBoundingClientRect){
      fn();
      return;
    }
    var y = window.scrollY || 0;
    var top = targetEl.getBoundingClientRect().top;
    if(lastTop === null) lastTop = top;

    var moved = (Math.abs(y - lastY) > 0.5) || (Math.abs(top - lastTop) > 0.5);
    lastY = y;
    lastTop = top;

    if(moved){
      stableSince = null;
    }else if(stableSince === null){
      stableSince = now;
    }

    var stableEnough = (stableSince !== null) && ((now - stableSince) >= SCROLL_SETTLE_MS);
    var timedOut = (now - start) >= SCROLL_MAX_WAIT_MS;
    if(stableEnough || timedOut){
      fn();
      return;
    }
    requestAnimationFrame(tick);
  }

  requestAnimationFrame(tick);
}

function focusSessionRow(iso){
  if(!iso)return;
  var row=document.querySelector('tr.row-click[data-iso="'+iso+'"]');
  if(!row)return;
  // Ensure collapsed (hide details row if open)
  var next=row.nextElementSibling;
  if(next && next.querySelector && next.querySelector('table.subtable')){
    next.style.display='none';
    var caret=row.querySelector('.caret');
    if(caret) caret.textContent='▸';
  }
  row.scrollIntoView({behavior:'smooth', block:'center'});

  if(row.__flashRemoveT) clearTimeout(row.__flashRemoveT);
  var token = (row.__flashToken||0) + 1;
  row.__flashToken = token;

  // Flash after scrolling actually settles (no fixed delay).
  afterScrollSettles(row, function(){
    if(row.__flashToken !== token) return;
    row.classList.remove('flash-row');
    // reflow to restart animation
    void row.offsetWidth;
    row.classList.add('flash-row');
    row.__flashRemoveT = setTimeout(function(){ row.classList.remove('flash-row'); }, FLASH_REMOVE_MS);
  });
}

// Click a Daily PnL bar -> jump to the corresponding session row
document.getElementById('dailyChart').addEventListener('click', function(evt){
  if(!window.__dayChart || !DATA || !Array.isArray(DATA.daily)) return;
  var pts = window.__dayChart.getElementsAtEventForMode(
    evt,
    'nearest',
    {intersect:true},
    true
  );
  if(!pts || !pts.length) return;
  var idx = pts[0].index;
  var iso = DATA.daily[idx] && DATA.daily[idx].date;
  if(iso) focusSessionRow(iso);
});

function parseCliTimeLabel(text){
  var s=(text||'').trim().toLowerCase().replace(/\s+/g,'');
  var m=s.match(/^(\d{1,2})(?::(\d{2}))?(am|pm)?$/);
  if(!m)return null;
  var h=parseInt(m[1],10);
  var mm=parseInt(m[2]||'0',10);
  var mer=m[3]||'';
  if(mer==='am'){
    if(h===12)h=0;
  }else if(mer==='pm'){
    if(h!==12)h=h+12;
  }
  if(h<0||h>23||mm<0||mm>59)return null;
  return (h<10?'0':'')+h+':' + (mm<10?'0':'')+mm;
}

function inferEntryExitTimes(){
  var sub=document.querySelector('.subtitle');
  var txt=sub?sub.textContent:'';
  var m=txt.match(/--trade-entry\s+([^\s]+)\s+--trade-exit\s+([^\s]+)/i);
  if(m){
    var en=parseCliTimeLabel(m[1]);
    var ex=parseCliTimeLabel(m[2]);
    if(en&&ex)return {entry:en, exit:ex};
  }
  return {entry:'', exit:''};
}

function buildSessionTradeHistory(){
  var tbody=document.getElementById('session-body');
  if(!tbody)return;
  if(!DATA||!Array.isArray(DATA.trades)||DATA.trades.length===0){
    return;
  }
  var times=inferEntryExitTimes();

  function cmp(a,b){return a<b?-1:a>b?1:0;}
  function toNum(x){return (typeof x==='number'&&isFinite(x))?x:null;}
  function toStr(x){return (x===null||x===undefined)?'':String(x);}

  var byKey={};
  DATA.trades.forEach(function(t){
    var k = (t.session_key!==undefined && t.session_key!==null && String(t.session_key).trim()!=='')
      ? String(t.session_key)
      : (t.date||'');
    if(!k)return;
    (byKey[k]=byKey[k]||[]).push(t);
  });
  function makeDetailsRow(iso, trades, colSpan){
    var detailsRow=document.createElement('tr');
    detailsRow.style.display='none';
    detailsRow.innerHTML=
      '<td class="details-cell" colspan="'+colSpan+'">'+
        '<div class="details-inner">'+
          '<div class="table-wrap">'+
            '<table class="subtable">'+
              '<thead><tr>'+
                '<th class="sortable" data-key="ticker">Ticker<span class="sort-ind"></span></th>'+
                '<th class="sortable" data-key="side">Side<span class="sort-ind"></span></th>'+
                '<th class="sortable" data-key="entryTime">Entry Time<span class="sort-ind"></span></th>'+
                '<th class="sortable" data-key="entryPrice">Entry Price<span class="sort-ind"></span></th>'+
                '<th class="sortable" data-key="exitTime">Exit Time<span class="sort-ind"></span></th>'+
                '<th class="sortable" data-key="exitPrice">Exit Price<span class="sort-ind"></span></th>'+
                '<th class="sortable" data-key="chgPct">Chg%<span class="sort-ind"></span></th>'+
                '<th class="sortable" data-key="pnl">PNL<span class="sort-ind"></span></th>'+
              '</tr></thead>'+
              '<tbody></tbody>'+
            '</table>'+
          '</div>'+
        '</div>'+
      '</td>';

    var subTbody=detailsRow.querySelector('tbody');
    var rowData=(trades||[]).map(function(t){
      var pe=(typeof t.entry==='number')?t.entry:null;
      var px=(typeof t.exit==='number')?t.exit:null;
      var chg=(pe&&px)?((px/pe-1)*100):null;
      var posLeg=(typeof t.pnl==='number')?t.pnl>=0:true;
      var d=(t.date||'');
      return {
        ticker:(t.coin||t.coin_id||''),
        side:(t.side||''),
        entryTime:(t.entry_label|| (times.entry? (d+' '+times.entry):d)),
        exitTime:(t.exit_label|| (times.exit? (d+' '+times.exit):d)),
        entryPrice:pe,
        exitPrice:px,
        chgPct:chg,
        pnl:toNum(t.pnl)||0,
        posLeg:posLeg
      };
    });

    function renderRows(rows){
      subTbody.innerHTML='';
      if(!rows || rows.length===0){
        subTbody.innerHTML='<tr><td colspan="8" class="td-muted">No trades for this session.</td></tr>';
        return;
      }
      rows.forEach(function(r){
        var tr=document.createElement('tr');
        tr.innerHTML=
          '<td><span class="pill trade">'+toStr(r.ticker)+'</span></td>'+
          '<td><span class="pill '+toStr(r.side)+'">'+toStr(r.side)+'</span></td>'+
          '<td class="td-muted">'+toStr(r.entryTime)+'</td>'+
          '<td>'+(r.entryPrice===null?'':r.entryPrice.toPrecision(6))+'</td>'+
          '<td class="td-muted">'+toStr(r.exitTime)+'</td>'+
          '<td>'+(r.exitPrice===null?'':r.exitPrice.toPrecision(6))+'</td>'+
          '<td style="color:'+pnlCellColor(r.chgPct===null?true:r.chgPct>=0)+'">'+(r.chgPct===null?'':fmtPct(r.chgPct))+'</td>'+
          '<td style="color:'+pnlCellColor(r.posLeg)+';font-weight:500">'+fmtPnl(r.pnl)+'</td>';
        subTbody.appendChild(tr);
      });
    }

    function sortRows(key, dir){
      var mul = dir==='desc' ? -1 : 1;
      var out = rowData.slice();
      out.sort(function(a,b){
        var av=a[key], bv=b[key];
        if(key==='entryPrice'||key==='exitPrice'||key==='chgPct'||key==='pnl'){
          av = (typeof av==='number'&&isFinite(av))?av: (key==='pnl'?0: -Infinity);
          bv = (typeof bv==='number'&&isFinite(bv))?bv: (key==='pnl'?0: -Infinity);
          return mul*(av-bv);
        }
        return mul*cmp(toStr(av).toLowerCase(), toStr(bv).toLowerCase());
      });
      renderRows(out);
    }

    function clearIndicators(){
      detailsRow.querySelectorAll('.sort-ind').forEach(function(x){x.textContent='';});
    }

    renderRows(rowData);

    detailsRow.querySelectorAll('th.sortable').forEach(function(th){
      th.addEventListener('click', function(){
        var key = th.getAttribute('data-key');
        var curKey = detailsRow.getAttribute('data-sort-key')||'';
        var curDir = detailsRow.getAttribute('data-sort-dir')||'asc';
        var nextDir = (curKey===key && curDir==='asc') ? 'desc' : 'asc';
        detailsRow.setAttribute('data-sort-key', key);
        detailsRow.setAttribute('data-sort-dir', nextDir);
        clearIndicators();
        var ind = th.querySelector('.sort-ind');
        if(ind) ind.textContent = nextDir==='asc' ? '▲' : '▼';
        sortRows(key, nextDir);
      });
    });
    return detailsRow;
  }

  // Attach expanders to existing session rows
  Array.prototype.slice.call(tbody.querySelectorAll('tr.row-click')).forEach(function(dayRow){
    var iso = dayRow.getAttribute('data-iso')||'';
    if(!iso)return;
    var trades = byKey[iso] || [];
    var colSpan = dayRow.children ? dayRow.children.length : 6;
    var detailsRow = makeDetailsRow(iso, trades, colSpan);
    dayRow.parentNode.insertBefore(detailsRow, dayRow.nextSibling);
    dayRow.addEventListener('click', function(){
      var open = detailsRow.style.display !== 'none';
      detailsRow.style.display = open ? 'none' : '';
      var caret = dayRow.querySelector('.caret');
      if(caret) caret.textContent = open ? '▸' : '▾';
    });
  });
}

buildSessionTradeHistory();

var tbody=document.getElementById('trade-body');
DATA.trades.slice(0,20).forEach(function(t){
  var pos=t.pnl>=0;
  tbody.innerHTML+='<tr>'+
    '<td class="td-muted">'+t.date+'</td>'+
    '<td><span class="pill trade">'+t.coin+'</span></td>'+
    '<td><span class="pill '+t.side+'">'+t.side+'</span></td>'+
    '<td>'+t.entry.toPrecision(4)+'</td>'+
    '<td>'+t.exit.toPrecision(4)+'</td>'+
    '<td style="color:'+pnlCellColor(pos)+'">'+fmtPct(t.ret)+'</td>'+
    '<td style="color:'+pnlCellColor(pos)+';font-weight:500">'+fmtPnl(t.pnl)+'</td>'+
    '</tr>';
});
</script>
</body>
</html>
"""


def _pnl_class(v: float) -> str:
    if v > 0:
        return "pos"
    if v < 0:
        return "neg"
    return "neu"


def _fmt_money_signed(v: float, decimals: int = 0) -> str:
    neg = v < 0
    av = abs(v)
    if decimals == 0:
        body = f"{int(round(av)):,}"
    else:
        body = f"{av:,.{decimals}f}"
    sign = "-" if neg else "+"
    return f"{sign}${body}"


def _fmt_money_plain(v: float, decimals: int = 2) -> str:
    """Dollar amount only (no +/−); use color elsewhere to show sign."""
    body = f"{abs(v):,.{decimals}f}"
    return f"${body}"


def _fmt_pct_signed(v: float, decimals: int = 1) -> str:
    pct = v * 100.0
    sign = "+" if pct > 0 else ("-" if pct < 0 else "")
    return f"{sign}{abs(pct):.{decimals}f}%"


def _date_range_pretty(start_iso: str, end_iso: str) -> str:
    d0 = datetime.strptime(start_iso, "%Y-%m-%d").date()
    d1 = datetime.strptime(end_iso, "%Y-%m-%d").date()
    return f"{d0.strftime('%b')} {d0.day} – {d1.strftime('%b')} {d1.day}, {d1.year}"


def _fmt_cli_num(x: Any) -> str:
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return str(x)
    if math.isfinite(xf) and abs(xf - round(xf)) < 1e-9:
        return str(int(round(xf)))
    s = f"{xf:.10f}".rstrip("0").rstrip(".")
    return s or "0"


def _hhmm_to_cli_label(hhmm: str) -> str:
    """Match run_baseline_longshort wall labels: 11:00 -> 11am, 23:00 -> 11pm, 9:30 -> 09:30."""
    try:
        hp, mp = hhmm.strip().split(":")
        h, m = int(hp), int(mp)
    except (ValueError, AttributeError):
        return hhmm
    if m != 0:
        return f"{h}:{m:02d}"
    h12 = h % 12
    if h12 == 0:
        h12 = 12
    suf = "am" if h < 12 else "pm"
    return f"{h12}{suf}"


def _meta_cli_subtitle_parts(meta: Dict[str, Any]) -> Dict[str, str]:
    """Rebuild subtitle_parts.cli_* from meta (older JSON without those keys)."""
    out: Dict[str, str] = {}
    s, e = meta.get("start_date"), meta.get("end_date")
    if s and e:
        out["cli_start_end"] = f"--start-date {s} --end-date {e}"
    te, tx = meta.get("trade_entry_local"), meta.get("trade_exit_local")
    if isinstance(te, str) and isinstance(tx, str) and te.strip() and tx.strip():
        out["cli_trade_times"] = (
            f"--trade-entry {_hhmm_to_cli_label(te)} --trade-exit {_hhmm_to_cli_label(tx)}"
        )
    tz = meta.get("trade_timezone")
    if isinstance(tz, str) and tz.strip():
        out["cli_timezone"] = f"--trade-timezone {tz.strip()}"
    pr = meta.get("params") or {}
    nt, nl = pr.get("notional_total_usd"), pr.get("notional_per_leg_usd")
    if nt is not None:
        out["cli_notional"] = f"--notional-total {_fmt_cli_num(nt)}"
    elif nl is not None:
        out["cli_notional"] = f"--notional-per-leg {_fmt_cli_num(nl)}"
    sk = pr.get("skip_threshold_abs")
    if sk is not None:
        out["cli_skip"] = f"--skip-threshold {sk}"
    mr = pr.get("mcap_rank_max")
    if mr is not None:
        out["cli_mcap_rank"] = f"--mcap-rank {int(mr)}"
    mx = pr.get("max_ticker_entries")
    if mx is not None:
        out["cli_max_tickers"] = f"--max-ticker-entries {int(mx)}"
    c24, c7 = pr.get("chg_24h_max_cap_pct"), pr.get("chg_7d_max_cap_pct")
    if c24 is not None and c7 is not None:
        out["cli_chg_caps"] = (
            f"--chg-24h-max-cap {_fmt_cli_num(c24)} --chg-7d-max-cap {_fmt_cli_num(c7)}"
        )
    if pr.get("ctrlpossize"):
        out["cli_ctrlpossize"] = "--ctrlpossize yes"
    if pr.get("skipsmallqty"):
        out["cli_skipsmallqty"] = "--skipsmallqty yes"
    if pr.get("reverse"):
        out["cli_reverse"] = "--reverse yes"
    return out


_SUBTITLE_CLI_KEYS = (
    "cli_start_end",
    "cli_trade_times",
    "cli_timezone",
    "cli_notional",
    "cli_skip",
    "cli_mcap_rank",
    "cli_max_tickers",
    "cli_chg_caps",
    "cli_ctrlpossize",
    "cli_skipsmallqty",
    "cli_blacklist",
    "cli_reverse",
)


def _build_subtitle(meta: Dict[str, Any]) -> str:
    parts = meta.get("subtitle_parts") or {}
    start = meta.get("start_date", "")
    end = meta.get("end_date", "")
    rng = _date_range_pretty(start, end) if start and end else ""
    session = parts.get("session", "")
    line = parts.get("notional_line")
    if line:
        notional = line
    else:
        n = parts.get("notional_per_side_usd")
        notional = f"${n:,.0f} notional per side" if isinstance(n, (int, float)) else ""
    filt = parts.get("volatility_skipper", "")
    bits = [rng, session, notional, filt]
    explicit = [
        parts[k]
        for k in _SUBTITLE_CLI_KEYS
        if isinstance(parts.get(k), str) and str(parts.get(k)).strip()
    ]
    if explicit:
        bits.extend(explicit)
    else:
        cli_map = _meta_cli_subtitle_parts(meta)
        for k in _SUBTITLE_CLI_KEYS:
            v = cli_map.get(k)
            if isinstance(v, str) and v.strip():
                bits.append(v)
    sep = " \u00a0\u00b7\u00a0 "  # nbsp middot nbsp, like sample
    return sep.join(b for b in bits if b)


def _chart_payload(doc: Dict[str, Any]) -> Dict[str, Any]:
    # `daily_overview` is rendered server-side into the HTML table, but we include it
    # in the JS payload so the client can key trade details by interval session.
    keys = ("equity", "daily", "monthly", "top", "worst", "trades", "daily_overview")
    out: Dict[str, Any] = {}
    for k in keys:
        v = doc.get(k)
        if k == "monthly":
            out[k] = v if isinstance(v, dict) else {}
        else:
            out[k] = v if isinstance(v, list) else []
    return out


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _date_dmy(iso_date: str) -> str:
    d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    return f"{d.day}/{d.month}/{d.year}"


def _daily_session_table_html(
    rows: List[Dict[str, Any]], *, strategy: str = ""
) -> str:
    """Date / session column | Trade | Reason | Total tickers (L/S) | PNL | Cumulative PNL.

    When ``strategy == interval_btc_1h4h_longshort`` or rows include ``session_label``,
    one row per interval session; first column uses ``session_label`` (local wall start).
    Otherwise one row per calendar day using ``date`` (YYYY-MM-DD).
    """
    is_interval = strategy == "interval_btc_1h4h_longshort" or any(
        isinstance(r, dict) and r.get("session_label") for r in rows
    )
    section_title = "Interval sessions" if is_interval else "Daily sessions"
    date_header = "Session start (local)" if is_interval else "Date"

    body_lines: List[str] = []
    cumulative = 0.0
    for row in rows:
        iso = str(row.get("date", ""))
        session_key = row.get("session_key")
        traded = bool(row.get("traded"))
        longs = int(row.get("longs", 0) or 0)
        shorts = int(row.get("shorts", 0) or 0)
        pnl = row.get("pnl_usd")
        reason_raw = row.get("reason")

        sl = row.get("session_label")
        if sl:
            date_cell = _html_escape(str(sl))
        elif iso:
            try:
                date_cell = _html_escape(_date_dmy(iso))
            except ValueError:
                date_cell = _html_escape(iso)
        else:
            date_cell = ""
        trade_cell = "Yes" if traded else "No"
        if reason_raw:
            reason_cell = (
                '<td style="color:var(--reason-text);font-size:12px;max-width:440px;white-space:normal">'
                f"{_html_escape(str(reason_raw))}"
                "</td>"
            )
        else:
            reason_cell = '<td style="color:var(--muted)">-</td>'
        if traded:
            total_ts = longs + shorts
            tick_cell = f"{total_ts} ({longs} L / {shorts} S)"
        else:
            tick_cell = "-"
        tick_cell = _html_escape(tick_cell)

        if pnl is None:
            pnl_cell = '<td style="color:var(--muted)">-</td>'
        else:
            pv = float(pnl)
            cumulative += pv
            col = "#1D9E75" if pv >= 0 else "#E24B4A"
            pnl_cell = (
                f'<td style="color:{col};font-weight:500">'
                f"{_html_escape(_fmt_money_signed(pv, 2))}"
                f"</td>"
            )

        if not traded:
            ccol = "var(--muted)"
        elif abs(cumulative) < 1e-12:
            ccol = "var(--muted)"
        else:
            ccol = "#1D9E75" if cumulative > 0 else "#E24B4A"
        cum_cell = (
            f'<td style="color:{ccol};font-weight:500">'
            f"{_html_escape(_fmt_money_plain(cumulative, 2))}"
            f"</td>"
        )

        key = str(session_key) if session_key else iso
        clickable = bool(key)
        row_cls = ' class="row-click"' if clickable else ""
        data_iso = f' data-iso="{_html_escape(key)}"' if clickable else ""
        caret_cell = (
            '<td class="td-muted"><span class="caret" aria-hidden="true">▸</span></td>'
            if clickable
            else '<td class="td-muted"></td>'
        )
        body_lines.append(
            f"<tr{row_cls}{data_iso}>"
            f"{caret_cell}"
            f'<td style="color:var(--muted)">{date_cell}</td>'
            f'<td>{trade_cell}</td>'
            f"{reason_cell}"
            f"<td>{tick_cell}</td>"
            f"{pnl_cell}"
            f"{cum_cell}"
            "</tr>"
        )

    tbody = (
        "\n".join(body_lines)
        if body_lines
        else "<tr><td colspan=\"6\" style=\"color:var(--muted)\">No rows</td></tr>"
    )
    return (
        '<div class="section">'
        f'<div class="section-title">{_html_escape(section_title)}</div>'
        '<div class="table-wrap">'
        "<table>"
        "<thead><tr>"
        "<th></th>"
        f"<th>{_html_escape(date_header)}</th><th>Trade</th><th>Reason</th>"
        "<th>Total Tickers (L/S)</th><th>PNL$</th>"
        "<th>Cumulative PNL</th>"
        "</tr></thead>"
        f'<tbody id="session-body">{tbody}</tbody>'
        "</table>"
        "</div>"
        "</div>"
    )


def _sessions_summary_table_html(rows: List[Dict[str, Any]]) -> str:
    """
    Build a summary table grouped by session start time (e.g. "09:00 New_York"),
    using `daily_overview` rows that include `session_label`.
    """
    groups: Dict[str, Dict[str, Any]] = {}

    for r in rows:
        sl = r.get("session_label")
        if not sl:
            continue
        parts = str(sl).strip().split()
        if len(parts) < 2:
            continue
        time_hhmm = parts[-2]
        tz_short = parts[-1]
        key = f"{time_hhmm} {tz_short}"

        g = groups.get(key)
        if g is None:
            g = {"net": 0.0, "n": 0, "wins": 0, "losses": 0, "max": None, "pnls": []}
            groups[key] = g

        traded = bool(r.get("traded"))
        pnl = r.get("pnl_usd")
        if (not traded) or pnl is None:
            continue
        pv = float(pnl)
        g["net"] += pv
        g["n"] += 1
        g["pnls"].append(pv)
        if pv > 0:
            g["wins"] += 1
        elif pv < 0:
            g["losses"] += 1
        if g["max"] is None or pv > g["max"]:
            g["max"] = pv

    if not any(v["n"] for v in groups.values()):
        return ""

    def _sort_key(k: str) -> tuple:
        try:
            hh, mm = k.split()[0].split(":")
            return (int(hh), int(mm), k)
        except Exception:
            return (999, 999, k)

    body_lines: List[str] = []
    for k in sorted(groups.keys(), key=_sort_key):
        g = groups[k]
        n = int(g["n"])
        if n <= 0:
            continue
        net = float(g["net"])
        avg = net / n if n else 0.0
        pnls = list(g.get("pnls") or [])
        pnls.sort()
        if pnls:
            mid = len(pnls) // 2
            median = pnls[mid] if len(pnls) % 2 == 1 else (pnls[mid - 1] + pnls[mid]) / 2.0
        else:
            median = 0.0
        if pnls:
            # Nearest-rank percentile: smallest x such that at least p% of values are <= x.
            # For p=80, this is the "top 20% threshold" value.
            idx80 = max(0, min(len(pnls) - 1, math.ceil(0.80 * len(pnls)) - 1))
            p80 = pnls[idx80]
        else:
            p80 = 0.0
        mx = float(g["max"]) if g["max"] is not None else 0.0
        wins = int(g["wins"])
        losses = int(g["losses"])
        wr = (wins / n) if n else 0.0
        wr_disp = f"{wr * 100:.1f}% ({wins}W / {losses}L)"

        net_col = "#1D9E75" if net > 0 else ("#E24B4A" if net < 0 else "var(--muted)")
        avg_col = "#1D9E75" if avg > 0 else ("#E24B4A" if avg < 0 else "var(--muted)")
        med_col = (
            "#1D9E75" if median > 0 else ("#E24B4A" if median < 0 else "var(--muted)")
        )
        p80_col = "#1D9E75" if p80 > 0 else ("#E24B4A" if p80 < 0 else "var(--muted)")

        body_lines.append(
            "<tr>"
            f"<td>{_html_escape(k)}</td>"
            f'<td style="text-align:right">{n}</td>'
            f'<td style="text-align:right">{_html_escape(wr_disp)}</td>'
            f'<td style="text-align:right;color:{net_col};font-weight:500">{_html_escape(_fmt_money_signed(net, 2))}</td>'
            f'<td style="text-align:right">{_html_escape(_fmt_money_signed(mx, 2))}</td>'
            f'<td style="text-align:right;color:{avg_col};font-weight:500">{_html_escape(_fmt_money_signed(avg, 2))}</td>'
            f'<td style="text-align:right;color:{med_col};font-weight:500">{_html_escape(_fmt_money_signed(median, 2))}</td>'
            f'<td style="text-align:right;color:{p80_col};font-weight:500">{_html_escape(_fmt_money_signed(p80, 2))}</td>'
            "</tr>"
        )

    if not body_lines:
        return ""

    return (
        '<div class="section">'
        '  <div class="section-title">Sessions Summary</div>'
        '  <div class="table-wrap">'
        "    <table>"
        "      <thead>"
        "        <tr>"
        "          <th>Session Interval</th>"
        '          <th style="text-align:right">Sessions Traded</th>'
        '          <th style="text-align:right">Win Rate</th>'
        '          <th style="text-align:right">Net PnL</th>'
        '          <th style="text-align:right">Highest PnL</th>'
        '          <th style="text-align:right">Avg PnL</th>'
        '          <th style="text-align:right">Median PnL</th>'
        '          <th style="text-align:right">P80 PnL</th>'
        "        </tr>"
        "      </thead>"
        f"      <tbody>{''.join(body_lines)}</tbody>"
        "    </table>"
        "  </div>"
        "</div>"
    )


def _daily_pnls_from_doc(doc: Dict[str, Any]) -> List[float]:
    daily = doc.get("daily") or []
    out: List[float] = []
    if not isinstance(daily, list):
        return out
    for row in daily:
        if isinstance(row, dict) and "pnl" in row:
            try:
                out.append(float(row["pnl"]))
            except (TypeError, ValueError):
                pass
    return out


def _sharpe_annual_from_pnls(
    pnls: List[float], *, periods_per_year: float = 365.0
) -> Optional[float]:
    n = len(pnls)
    if n < 2:
        return None
    mean = sum(pnls) / n
    var = sum((x - mean) ** 2 for x in pnls) / (n - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std < 1e-12:
        return None
    return (mean / std) * math.sqrt(periods_per_year)


def render_html(doc: Dict[str, Any], *, title: str) -> str:
    """Fill HTML_TEMPLATE. ``title`` is the main heading, <title> text, and first segment of the subtitle (before meta)."""
    meta = doc.get("meta") or {}
    summ = meta.get("summary") or {}

    total = float(summ.get("total_pnl_usd", 0.0))
    win_rate = float(summ.get("win_rate", 0.0))
    active = int(summ.get("active_days", 0))
    cal = int(summ.get("calendar_days", 0))
    mdd = float(summ.get("max_drawdown_usd", 0.0))
    avg_d = float(summ.get("avg_daily_pnl_usd", 0.0))
    best = float(summ.get("best_day_pnl_usd", 0.0))
    worst = float(summ.get("worst_day_pnl_usd", 0.0))
    ron = float(summ.get("return_on_notional", 0.0))

    wd = summ.get("win_days")
    ld = summ.get("loss_days")
    if wd is not None and ld is not None:
        win_days, loss_days = int(wd), int(ld)
    else:
        pnls_infer = _daily_pnls_from_doc(doc)
        win_days = sum(1 for p in pnls_infer if p > 0)
        loss_days = sum(1 for p in pnls_infer if p < 0)
    wins_losses = f"{win_days}W / {loss_days}L"

    sharpe_raw = summ.get("sharpe_ratio")
    sharpe: Optional[float]
    if sharpe_raw is not None:
        try:
            sharpe = float(sharpe_raw)
        except (TypeError, ValueError):
            sharpe = _sharpe_annual_from_pnls(_daily_pnls_from_doc(doc))
    else:
        sharpe = _sharpe_annual_from_pnls(_daily_pnls_from_doc(doc))
    sharpe_disp = f"{sharpe:.2f}" if sharpe is not None else "—"

    payload = _chart_payload(doc)
    data_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    # Avoid closing the HTML script block if a string ever contains </script>
    data_json = data_json.replace("<", "\\u003c")

    display_title = (title or "").strip() or "Backtest dashboard"
    html_title = re.sub(r"[^\w\s\-—]", "", display_title)[:80] or "Backtest dashboard"
    meta_sub = _build_subtitle(meta)
    sep = " \u00a0\u00b7\u00a0 "  # nbsp middot nbsp, match _build_subtitle
    esc_name = _html_escape(display_title)
    subtitle = esc_name + (sep + _html_escape(meta_sub) if meta_sub else "")

    overview = doc.get("daily_overview")
    if not isinstance(overview, list):
        overview = []
    strategy = str(meta.get("strategy") or "")
    session_section = _daily_session_table_html(overview, strategy=strategy)
    if strategy == "interval_btc_1h4h_longshort" or any(
        isinstance(r, dict) and r.get("session_label") for r in overview
    ):
        session_section = session_section + _sessions_summary_table_html(overview)

    html = HTML_TEMPLATE
    reps = {
        "__HTML_TITLE__": _html_escape(html_title),
        "__TITLE__": esc_name,
        "__SUBTITLE__": subtitle,
        "__CLS_TOTAL__": _pnl_class(total),
        "__TOTAL_PNL__": _fmt_money_signed(total, 0),
        "__WIN_RATE__": f"{win_rate * 100:.1f}%",
        "__WINS_LOSSES__": wins_losses,
        "__ACTIVE_DAYS__": f"{active} / {cal}",
        "__MAX_DD__": _fmt_money_signed(mdd, 0),
        "__CLS_AVG__": _pnl_class(avg_d),
        "__AVG_DAILY__": _fmt_money_signed(avg_d, 2),
        "__BEST_DAY__": _fmt_money_signed(best, 0),
        "__WORST_DAY__": _fmt_money_signed(worst, 0),
        "__CLS_RON__": _pnl_class(ron),
        "__RETURN_ON_NOTIONAL__": _fmt_pct_signed(ron, 2),
        "__SHARPE__": sharpe_disp,
        "__DATA_JSON__": data_json,
        "__DAILY_SESSION_SECTION__": session_section,
    }
    for k, v in reps.items():
        html = html.replace(k, v)
    return html


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate dashboard HTML from baseline JSON.")
    p.add_argument(
        "--input",
        "-i",
        default=str(ROOT / "out" / "baseline_dashboard.json"),
        help="Path to dashboard JSON",
    )
    p.add_argument(
        "--output",
        "-o",
        default="",
        help="Write HTML here (default: same path as input with .html)",
    )
    p.add_argument(
        "--title",
        default="",
        help="Main page heading and <title> (default: input JSON filename without .json)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    in_path = Path(args.input)
    if not in_path.is_file():
        raise SystemExit(f"Input not found: {in_path}")

    doc = json.loads(in_path.read_text(encoding="utf-8"))
    out_path = Path(args.output) if args.output else in_path.with_suffix(".html")

    title = (args.title or "").strip() or in_path.stem
    html = render_html(doc, title=title)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(str(out_path))


if __name__ == "__main__":
    main()
