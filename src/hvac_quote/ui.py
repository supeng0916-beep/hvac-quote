"""服务端 HTML 页面。

客户页面只呈现项目、范围、金额和可执行动作；内部字段留在 JSON 与内部示例页。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import escape

from .help_content import HELP_ARTICLES, WORKFLOW_STEPS
from .presentation import build_groups, equipment_rows as presentation_equipment_rows
from .money import format_quantity
from .projects import Project, QuoteRecord
from .vision import ALLOWED_CATEGORIES, ALLOWED_MEDIA, ALLOWED_ROLES

STYLE = """
:root{--bg:#f5f5f7;--panel:#fff;--ink:#1d1d1f;--muted:#6e6e73;--soft:#8a8a90;
--line:#e3e4e6;--line-strong:#d2d3d7;--nav:#141821;--nav-ink:#aeb4c0;--nav-strong:#fff;
--blue:#0066cc;--blue-dark:#0055ad;--teal:#0f766e;--orange:#f26a21;--warn:#b45309;
--danger:#b91c1c;--wash:#eef2f7;--radius:18px;--shadow:0 18px 54px rgba(29,29,31,.08)}
*{box-sizing:border-box}
[hidden]{display:none !important}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei","Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--blue);text-decoration:none}
a:hover{color:var(--blue-dark)}
button,input,textarea,select{font:inherit}
button{cursor:pointer}
img{max-width:100%}
h1,h2,h3,h4{color:var(--ink);letter-spacing:-.03em;line-height:1.28}
p{margin:0 0 10px}
code{font-family:"SFMono-Regular",Consolas,monospace;font-size:12px}
.wrap{max-width:1160px;margin:0 auto;padding:0 24px}
.brand-mark{width:36px;height:36px;border-radius:10px;background:var(--nav);color:#fff;display:grid;place-items:center;font-size:11px;font-weight:750;letter-spacing:.6px;flex:none;position:relative}
.brand-mark::after{content:"";position:absolute;right:5px;top:5px;width:5px;height:5px;border-radius:1.5px;background:var(--orange)}
.identity{display:flex;align-items:center;gap:11px;color:var(--ink);text-decoration:none}
.identity:hover{color:var(--ink)}
.identity-text{font-size:17px;font-weight:700;letter-spacing:.4px;line-height:1.2}
.identity-text small{display:block;font-size:11px;font-weight:400;color:var(--muted);letter-spacing:0;margin-top:3px}
.public-header{max-width:1200px;margin:0 auto;padding:24px 24px 0;display:flex;align-items:center;justify-content:space-between;gap:18px}
.public-nav{display:flex;align-items:center;gap:18px}
.text-link{font-size:13px;color:#424247}
.text-link:hover{color:var(--blue)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:9px;min-height:44px;padding:0 20px;border:1px solid var(--blue);border-radius:10px;background:var(--blue);color:#fff;font-size:14px;font-weight:600;text-decoration:none;box-shadow:0 4px 12px rgba(0,102,204,.12);transition:transform .16s cubic-bezier(.23,1,.32,1),background-color .16s}
.btn:hover{background:var(--blue-dark);color:#fff;transform:translateY(-1px)}
.btn:active{transform:translateY(0) scale(.99)}
.btn:disabled{opacity:.45;cursor:not-allowed;transform:none}
.btn--small{min-height:36px;padding:0 14px;font-size:13px;border-radius:8px}
.btn--block{width:100%}
.btn--ghost{background:#fff;color:#424247;border-color:var(--line-strong);box-shadow:none}
.btn--ghost:hover{background:#f7f7f9;color:var(--ink)}
.btn--danger{background:#fff;color:var(--danger);border-color:#f0c4c4;box-shadow:none}
.btn--danger:hover{background:#fef2f2;color:var(--danger)}
.eyebrow{margin:0 0 10px;font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);font-weight:650}
.lead{color:var(--muted);font-size:15px;line-height:1.8;max-width:720px}
.muted{color:var(--muted);font-size:13px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:24px;box-shadow:0 1px 2px rgba(16,24,40,.03)}
.card[id],details[id],li[id^="drawing-issue-"]{scroll-margin-top:78px}
.card h2{font-size:18px;margin:0 0 8px}
.card h3{font-size:15px;margin:0 0 8px}
.recon-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:12px 0 4px}
.recon-card .notice{margin:10px 0 0}
.guide-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:12px}
.guide-grid>div{background:#f8fafc;border:1px solid #e6ebf2;border-radius:11px;padding:12px 14px}
.guide-grid strong{display:block;font-size:13px;margin-bottom:3px}
.guide-grid p{margin:0;color:var(--muted);font-size:12px;line-height:1.7}
.advanced-block>summary{display:flex;align-items:center;gap:10px;flex-wrap:wrap;cursor:pointer;list-style:none}
.advanced-block>summary::-webkit-details-marker{display:none}
.advanced-block>summary::before{content:"▸";color:var(--blue);font-size:12px}
.advanced-block[open]>summary::before{content:"▾"}
.advanced-block>summary h2{font-size:16px;margin:0}
.advanced-block[open]>summary{margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid #edf0f4}
.badge{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap;background:var(--wash);color:#475569;border:1px solid #dbe3ec}
.badge--ok{background:#ecfdf5;color:var(--teal);border-color:#a7f3d0}
.badge--warn{background:#fff7e6;color:var(--warn);border-color:#f3d19e}
.badge--info{background:#eef5ff;color:#1d4ed8;border-color:#cfe0f8}
.notice{background:#fff7e6;border-left:4px solid #d97706;padding:11px 14px;border-radius:8px;font-size:13px;margin:12px 0}
.notice--info{background:#eff6ff;border-left-color:#2563eb}
.notice--success{background:#ecfdf5;border-left-color:#059669;color:#065f46}
.notice--danger{background:#fef2f2;border-left-color:var(--danger);color:#991b1b}
.alert{background:#fef2f2;border:1px solid #fecaca;color:#991b1b;padding:12px 14px;border-radius:10px;margin:0 0 16px;font-size:13px}
.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.field{display:flex;flex-direction:column;gap:6px;font-size:13px}
.field>span{font-weight:600;color:#334155}
.field input,.field textarea,.field select{width:100%;padding:11px 12px;border:1px solid #cbd2dc;border-radius:9px;background:#fff;color:var(--ink);outline:none;transition:border-color .15s,box-shadow .15s}
.field input:focus,.field textarea:focus,.field select:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(0,102,204,.12)}
.field textarea{resize:vertical;min-height:70px}
.field--wide{grid-column:1/-1}
.check-list{display:flex;flex-direction:column;gap:6px;max-height:210px;overflow:auto;border:1px solid #e2e8f0;border-radius:10px;padding:10px 12px;background:#fbfcfe}
.check-line{display:flex;align-items:center;gap:6px;font-size:13px;line-height:1.6}
.check-line input{width:auto;margin:0}
.muted-row{background:#fafafa;color:#64748b}
.table input[type="number"]{width:100%;min-width:78px;padding:7px 8px;border:1px solid #cbd2dc;border-radius:7px}
.table select{max-width:300px;padding:7px 8px;border:1px solid #cbd2dc;border-radius:7px;background:#fff}
.table .check-cell{text-align:center}
.table .check-cell input{width:18px;height:18px}
.draft-table{table-layout:fixed}
.draft-table th,.draft-table td{padding:9px 8px}
.draft-table td,.draft-table th{overflow-wrap:anywhere}
.draft-table select{width:100%;max-width:none;min-width:0}
.draft-table input[type="checkbox"]{width:16px;min-width:0;height:16px;padding:0;margin:0;border:0}
.draft-table .muted{display:inline-block;line-height:1.5}
.stack-check{display:flex;flex-direction:column;gap:5px;font-size:12px;color:#475569}
.stack-check label{display:flex;align-items:center;gap:4px;white-space:nowrap}
.stack-check input{width:16px;height:16px;margin:0}
.water-inputs{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-top:7px;padding:7px 8px;background:#f0f7ff;border:1px solid #d6e6fb;border-radius:7px;font-size:11.5px;color:#475569}
.water-inputs input{width:82px;min-width:0;padding:4px 6px;border:1px solid #cbd2dc;border-radius:5px;font-size:11.5px}
.water-inputs .water-unit{width:54px}
.form-note{margin:16px 0 0;color:var(--soft);font-size:12px;line-height:1.7}
.form-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:20px}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px;margin-top:12px}
table.table{width:100%;border-collapse:collapse;font-size:13px;background:#fff}
.table th,.table td{padding:10px 12px;text-align:left;border-bottom:1px solid #edf0f4;vertical-align:middle}
.table th{background:#f7f8fa;color:#52525b;font-size:12px;font-weight:650;white-space:nowrap}
.table tr:last-child td{border-bottom:0}
.table td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.table input{width:100%;min-width:88px;padding:8px 9px;border:1px solid #cbd2dc;border-radius:7px}
.kpis{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-top:16px}
.kpi{background:#f8f9fb;border:1px solid #edf0f4;border-radius:12px;padding:14px 16px}
.kpi small{display:block;color:var(--muted);font-size:12px;margin-bottom:4px}
.kpi strong{font-size:20px;color:var(--ink);letter-spacing:-.02em}
.kpi--accent{background:#eff6ff;border-color:#d8e6fb}
.kpi--accent strong{color:var(--blue)}
.steps{display:flex;gap:7px;flex-wrap:wrap;margin:16px 0 4px}
.step{padding:6px 12px;border-radius:999px;background:#e9edf3;color:#64748b;font-size:12px}
.step.active{background:var(--nav);color:#fff;font-weight:650}
.step.done{background:#d1fae5;color:#065f46}
.workflow{background:#fff;border:1px solid var(--line);border-radius:16px;padding:18px 20px;margin:0 0 20px}
.workflow-head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap}
.workflow-head h2{font-size:17px;margin:0 0 3px}
.workflow-head p{margin:0;color:var(--muted);font-size:12px}
.workflow-steps{list-style:none;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:15px 0 0;padding:0}
.workflow-step{display:flex;flex-direction:column;border:1px solid #e6e9ef;border-radius:12px;padding:12px 13px;background:#fafbfc;min-height:112px}
.workflow-step .num{display:inline-grid;place-items:center;width:23px;height:23px;border-radius:7px;background:#e9edf3;color:#64748b;font-size:11px;font-weight:750;margin-bottom:8px}
.workflow-step strong{display:block;font-size:13px;line-height:1.45}
.workflow-step small{display:block;color:var(--muted);font-size:11px;line-height:1.65;margin-top:3px}
.workflow-step a{display:inline-flex;margin-top:auto;padding-top:8px;font-size:11px;font-weight:650}
.workflow-step.done{border-color:#c7ead9;background:#f4fbf7}
.workflow-step.done .num{background:#d1fae5;color:#065f46}
.workflow-step.active{border-color:#b9d4f8;background:#f5f9ff;box-shadow:inset 0 0 0 1px #dbeafe}
.workflow-step.active .num{background:var(--blue);color:#fff}
.inline-form{display:inline;margin:0}
.file-actions .btn--step{min-height:32px;padding:0 12px;font-size:12px}
.file-actions .file-state{display:inline-flex;align-items:center;gap:5px;font-weight:650;color:#047857}
.page-head{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;margin:6px 0 24px;flex-wrap:wrap}
.page-head h1{margin:0 0 8px;font-size:clamp(27px,3vw,36px);font-weight:680}
.page-head .lead{margin:0}
.page-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.back-link{display:inline-flex;align-items:center;gap:8px;font-size:13px;font-weight:650;color:#2f3035;margin-bottom:20px;padding:9px 16px;border:1px solid #cbd2dc;border-radius:999px;background:#fff;box-shadow:0 2px 6px rgba(16,24,40,.06)}
.back-link:hover{color:var(--blue);border-color:#9fbde6;background:#f8fbff;box-shadow:0 4px 12px rgba(16,24,40,.08)}
.back-link:focus-visible{outline:3px solid #cfe2ff;outline-offset:2px}
.section-head{display:flex;align-items:center;justify-content:space-between;gap:16px;margin:30px 0 12px}
.section-head h2{font-size:19px;margin:0}
.empty{padding:44px 24px;text-align:center;color:var(--muted)}
.empty h2{font-size:20px;margin:0 0 8px;color:var(--ink)}
.empty p{margin:0 auto 18px;max-width:460px}
.site-foot{max-width:1200px;margin:0 auto;padding:30px 24px 36px;display:flex;align-items:center;gap:18px;color:var(--soft);font-size:11px;border-top:1px solid var(--line);margin-top:48px}
.site-foot .company{display:flex;align-items:center;gap:9px;color:#414146;font-size:12px;white-space:nowrap}
.logo-crop{position:relative;width:38px;height:27px;overflow:hidden;flex:none}
.logo-crop img{position:absolute;width:86px;max-width:none;left:-25px;top:-21px;mix-blend-mode:multiply}
.site-foot .slogan{margin:0}
.site-foot .product{margin-left:auto;white-space:nowrap}
.loading-overlay{position:fixed;inset:0;z-index:90;background:rgba(245,245,247,.94);backdrop-filter:blur(12px);display:grid;place-items:center;padding:24px}
.loading-overlay[hidden]{display:none}
.loading-card{position:relative;width:min(460px,100%);background:#fff;border:1px solid var(--line);border-radius:22px;padding:30px 32px 24px;text-align:left;box-shadow:0 28px 80px rgba(29,29,31,.14);animation:rise .3s ease both;overflow:hidden}
.loading-card::before{content:"";position:absolute;inset:0 0 auto;height:4px;background:linear-gradient(90deg,#0066cc,#8cc4ff,#0066cc);background-size:200% 100%;animation:loadingGlow 3s linear infinite}
.loading-eyebrow{margin:0 0 8px;color:var(--blue);font-size:10px;font-weight:750;letter-spacing:.12em;text-transform:uppercase}
.loading-card h2{margin:0;font-size:21px;font-weight:680;letter-spacing:-.03em}
.loading-card .loading-note{color:var(--muted);font-size:12.5px;font-weight:400;line-height:1.75;margin:10px 0 0}
.loading-track{height:8px;background:#edf1f6;border-radius:999px;overflow:hidden;margin:24px 0 14px}
.loading-track span{display:block;height:100%;width:34%;border-radius:999px;background:linear-gradient(90deg,#8cc4ff,#0066cc);box-shadow:0 0 12px rgba(0,102,204,.32);animation:loadingSweep 3.2s cubic-bezier(.45,.05,.25,1) infinite}
.loading-meta{display:flex;align-items:center;justify-content:space-between;gap:16px;color:var(--soft);font-size:11px}
@keyframes loadingSweep{0%{transform:translateX(-115%)}58%{transform:translateX(185%)}100%{transform:translateX(330%)}}
@keyframes loadingGlow{0%{background-position:0 0}100%{background-position:200% 0}}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}

/* 公共首页 */
.landing{position:relative;overflow:hidden;isolation:isolate;min-height:100dvh;display:flex;flex-direction:column}
.landing::before{content:"";position:absolute;z-index:-2;inset:-20% -12% auto;height:86vh;background:radial-gradient(ellipse at 50% 16%,#e4eefb 0,#f5f5f7 58%,transparent 76%)}
.landing-orbit{position:absolute;z-index:-1;top:90px;left:50%;width:min(1120px,124vw);height:min(560px,62vw);border:1px solid #d7dde7;border-radius:50%;transform:translateX(-50%) rotate(-14deg);mask-image:linear-gradient(90deg,transparent,#000 16%,#000 84%,transparent);pointer-events:none}
.landing-orbit::after{content:"";position:absolute;inset:12% -6%;border:1px solid #e1e6ee;border-radius:50%;transform:rotate(24deg)}
.landing-hero{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:76px 24px 42px}
.landing-hero h1{margin:0 0 22px;font-size:clamp(38px,6.4vw,78px);line-height:1.14;letter-spacing:-.06em;font-weight:690;animation:rise .7s cubic-bezier(.23,1,.32,1) both}
.landing-hero h1 .accent{color:var(--blue)}
.landing-hero .lead{margin:0 0 32px;font-size:16px;max-width:620px;animation:rise .7s .08s cubic-bezier(.23,1,.32,1) both}
.landing-actions{display:flex;align-items:center;gap:18px;flex-wrap:wrap;justify-content:center;animation:rise .7s .14s cubic-bezier(.23,1,.32,1) both}
.landing-cta{min-height:54px;padding:0 28px;border-radius:999px;font-size:15px}
.landing-trust{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px;width:min(760px,100%);margin-top:56px;text-align:left;animation:rise .7s .22s cubic-bezier(.23,1,.32,1) both}
.landing-trust div{padding:16px 18px;border:1px solid #e3e7ee;background:rgba(255,255,255,.66);border-radius:14px}
.landing-trust strong{display:block;font-size:14px;margin-bottom:3px}
.landing-trust span{color:var(--muted);font-size:12px}
.landing-flow{max-width:1040px;margin:0 auto;padding:26px 24px 72px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px;width:100%}
.landing-flow article{background:#fff;border:1px solid var(--line);border-radius:16px;padding:22px}
.landing-flow article span{font-size:11px;color:var(--blue);font-weight:700;letter-spacing:.08em}
.landing-flow h2{font-size:17px;margin:8px 0 6px}
.landing-flow p{color:var(--muted);font-size:13px;margin:0;line-height:1.7}

/* 登录与首次初始化 */
.auth-body{background:#f5f5f7;min-height:100dvh;display:flex;flex-direction:column}
.auth-shell{flex:1;width:100%;max-width:1160px;margin:0 auto;padding:0 24px}
.auth-grid{display:grid;grid-template-columns:minmax(0,1.08fr) minmax(340px,.92fr);gap:clamp(36px,7vw,100px);align-items:center;padding:64px 0 54px;min-height:calc(100dvh - 210px)}
.auth-story h1{font-size:clamp(32px,3.6vw,50px);line-height:1.2;letter-spacing:-.055em;margin:14px 0 16px;font-weight:660}
.auth-story .lead{font-size:15px;max-width:440px}
.proof-list{list-style:none;margin:28px 0 0;padding:0;display:grid;gap:12px;max-width:440px}
.proof-list li{display:flex;gap:11px;align-items:flex-start;color:#4b4b50;font-size:13px}
.proof-list b{display:grid;place-items:center;width:20px;height:20px;border-radius:6px;background:#e8eef7;color:var(--blue);font-size:11px;flex:none;margin-top:1px}
.auth-card{background:#fff;border:1px solid var(--line);border-radius:22px;padding:38px;box-shadow:var(--shadow)}
.auth-card h1{margin:12px 0 8px;font-size:30px;font-weight:660;letter-spacing:-.05em}
.auth-card .sub{color:var(--muted);margin:0 0 26px;font-size:14px}
.auth-card form{display:grid;gap:18px}
.auth-card .btn{margin-top:2px;min-height:48px}
.auth-foot{margin:0;text-align:center;color:var(--soft);font-size:12px;line-height:1.8}
.setup-hint{background:#f8f9fb;border:1px solid #edf0f4;border-radius:10px;padding:12px 14px;color:#52525b;font-size:12px;line-height:1.7}

/* 工作台 */
.app-body{background:var(--bg)}
.app-shell{display:flex;min-height:100dvh}
.sidebar{width:224px;flex:none;background:var(--nav);color:var(--nav-ink);display:flex;flex-direction:column;position:sticky;top:0;height:100dvh}
.sidebar-brand{display:flex;align-items:center;gap:10px;color:#fff;padding:24px 20px 20px;text-decoration:none}
.sidebar-brand:hover{color:#fff}
.sidebar-brand .brand-mark{width:34px;height:34px;background:#fff;color:var(--nav);font-size:10px}
.sidebar-brand .brand-mark::after{background:var(--orange)}
.sidebar-brand strong{display:block;font-size:19px;letter-spacing:2.5px;line-height:1.2}
.sidebar-brand small{display:block;font-size:10px;color:#8f96a4;letter-spacing:1px;margin-top:3px}
.side-nav{display:flex;flex-direction:column;gap:3px;padding:11px 10px;border-top:1px solid #262b36;border-bottom:1px solid #262b36}
.side-nav a{display:flex;align-items:center;gap:10px;padding:10px 13px;border-radius:8px;color:var(--nav-ink);font-size:13px;text-decoration:none}
.side-nav a:hover{background:#1e2430;color:#fff}
.side-nav a.active{background:#232a37;color:#fff;font-weight:600;box-shadow:inset 3px 0 var(--blue)}
.nav-icon{width:16px;text-align:center;font-size:14px;opacity:.8}
.sidebar-project{margin:18px 13px 0;padding:14px;border:1px solid #2a303d;border-radius:10px;background:#191e28}
.sidebar-project span{display:block;font-size:10px;color:#828a99;letter-spacing:.08em;margin-bottom:5px}
.sidebar-project strong{display:block;font-size:12px;color:#e8eaee;font-weight:550;line-height:1.55;overflow-wrap:anywhere}
.sidebar-foot{margin-top:auto;padding:18px 20px 22px;border-top:1px solid #262b36;font-size:10px;line-height:1.7;color:#7d8492}
.sidebar-foot .company{display:flex;align-items:center;gap:7px;color:#c8ccd4;font-size:11px;margin-bottom:7px}
.sidebar-foot .logo-crop{width:30px;height:22px;filter:brightness(3)}
.sidebar-foot .logo-crop img{width:70px;left:-20px;top:-17px;mix-blend-mode:screen}
.sidebar-foot p{margin:0 0 8px}
.sidebar-foot .product{color:#6f7785}
.app-main{flex:1;min-width:0;display:flex;flex-direction:column}
.appbar{min-height:60px;background:rgba(255,255,255,.86);backdrop-filter:blur(12px);border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;gap:16px;padding:0 28px;position:sticky;top:0;z-index:20}
.crumbs{display:flex;align-items:center;gap:8px;color:var(--soft);font-size:12px;min-width:0}
.crumbs a{color:var(--muted)}
.crumbs .sep{color:#c5c6cb}
.crumbs .current{color:#3f3f46;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.appbar-actions{display:flex;align-items:center;gap:16px;flex:none}
.help-link{font-size:12px;color:var(--muted)}
.user-chip{display:flex;align-items:center;gap:8px}
.user-avatar{width:29px;height:29px;border-radius:50%;display:grid;place-items:center;background:#e8eef7;color:var(--blue);font-size:12px;font-weight:700}
.user-name{font-size:12px;color:#3f3f46;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.text-button{border:0;background:none;color:var(--muted);font-size:12px;padding:5px 0}
.text-button:hover{color:var(--danger)}
.content{width:100%;max-width:1120px;margin:0 auto;padding:30px 28px 48px;flex:1}
.content-footer{max-width:1120px;width:100%;margin:0 auto;padding:20px 28px 28px;border-top:1px solid var(--line);display:flex;align-items:center;gap:14px;color:var(--soft);font-size:11px}
.content-footer .company{display:flex;align-items:center;gap:8px;color:#55555b;font-size:11px;white-space:nowrap}
.content-footer .slogan{margin:0}
.content-footer .product{margin-left:auto;white-space:nowrap}

/* 项目 */
.stat-row{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin:0 0 26px}
.stat{background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px 18px}
.stat small{display:block;color:var(--muted);font-size:11px;margin-bottom:4px}
.stat strong{font-size:24px;letter-spacing:-.03em}
.project-toolbar{display:flex;align-items:center;justify-content:space-between;gap:14px;margin:0 0 16px;flex-wrap:wrap}
.project-filter{max-width:280px;width:100%;padding:9px 12px;border:1px solid #cbd2dc;border-radius:9px;background:#fff}
.project-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.project-card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:20px;position:relative;transition:transform .16s,box-shadow .16s,border-color .16s;display:flex;flex-direction:column;min-height:184px}
.project-card:hover{transform:translateY(-2px);box-shadow:0 14px 34px rgba(29,29,31,.07);border-color:#d2d7e0}
.project-card .card-top{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}
.project-card h2{font-size:18px;margin:0 0 4px}
.project-card .customer{color:var(--muted);font-size:12px;margin:0 0 10px}
.project-card .note{color:#6b6b70;font-size:12px;line-height:1.7;margin:0 0 14px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.project-card .card-foot{margin-top:auto;display:flex;align-items:center;justify-content:space-between;gap:10px;padding-top:12px;border-top:1px solid #edf0f4;font-size:12px;color:var(--muted)}
.project-card .arrow{color:var(--blue);font-weight:600}
.project-card .card-main{color:inherit;display:block;text-decoration:none;height:100%}
.project-card .card-main:hover{color:inherit}
.form-card{max-width:680px;background:#fff;border:1px solid var(--line);border-radius:18px;padding:28px}
.form-card h1{font-size:28px;margin:0 0 8px}
.form-card .lead{margin-bottom:22px}
.meta-strip{display:flex;gap:22px;flex-wrap:wrap;background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px 18px;margin:0 0 26px;color:var(--muted);font-size:12px}
.meta-strip strong{color:var(--ink);font-weight:600}
.quote-list{display:flex;flex-direction:column;gap:12px}
.quote-row{display:grid;grid-template-columns:minmax(0,1fr) 150px 230px;gap:18px;align-items:center;background:#fff;border:1px solid var(--line);border-radius:14px;padding:17px 19px}
.quote-row h3{font-size:15px;margin:6px 0 3px}
.quote-row .meta{color:var(--muted);font-size:11px;margin:0}
.quote-total{text-align:right;font-size:18px;font-weight:650;font-variant-numeric:tabular-nums}
.quote-total small{display:block;color:var(--muted);font-size:10px;font-weight:400;margin-bottom:3px}
.quote-actions{display:flex;justify-content:flex-end;gap:12px;flex-wrap:wrap;font-size:12px;align-items:center}
.quote-actions a{font-weight:550}
.detail-list{display:grid;gap:9px;margin:14px 0 0;padding:0;list-style:none;color:#4b4b50;font-size:13px}
.detail-list li{display:flex;gap:9px;align-items:flex-start}
.detail-list li::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--blue);flex:none;margin-top:8px}
.quote-head{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;flex-wrap:wrap;margin-bottom:20px}
.quote-head h1{font-size:clamp(26px,3vw,34px);margin:8px 0 6px}
.quote-head .meta{color:var(--muted);font-size:12px;margin:0}
.quote-summary{display:grid;grid-template-columns:1.15fr .85fr;gap:16px;margin-bottom:16px}
.total-card{background:linear-gradient(145deg,#10141d,#1b2230);color:#fff;border-radius:18px;padding:24px;border:1px solid #252c3a}
.total-card small{display:block;color:#9aa3b2;font-size:11px;margin-bottom:6px}
.total-card strong{display:block;font-size:clamp(28px,4vw,42px);letter-spacing:-.04em;line-height:1.15}
.total-card .tax-line{display:flex;gap:20px;flex-wrap:wrap;margin-top:16px;padding-top:14px;border-top:1px solid #2b3343;color:#c3c9d4;font-size:12px}
.total-card .tax-line b{color:#fff;font-weight:600}
.side-card{background:#fff;border:1px solid var(--line);border-radius:18px;padding:22px}
.side-card h3{font-size:14px;margin:0 0 10px}
.side-card p{font-size:12px;color:var(--muted);line-height:1.7;margin:0 0 10px}
.side-card .btn{width:100%;margin-top:6px}
.group-card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:20px;margin-bottom:14px}
.group-card h2{font-size:16px;margin:0 0 4px}
.group-card .sum{margin-left:auto;font-size:14px;font-weight:650;font-variant-numeric:tabular-nums}
.group-head{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.excluded{display:flex;flex-direction:column;gap:8px;margin:10px 0 0;padding-left:20px;color:#52525b;font-size:13px}
.excluded li{line-height:1.7}
.issue-details{margin-top:12px;padding-top:10px;border-top:1px solid #edf0f4}
.issue-details>summary{cursor:pointer;color:var(--blue);font-size:12.5px}
.download-row{display:flex;gap:12px;flex-wrap:wrap;margin-top:14px}
.download-row .btn{min-height:42px}
.step-layout{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:18px;align-items:start}
.step-main{min-width:0}
.step-help{position:sticky;top:82px;background:#fff;border:1px solid var(--line);border-radius:16px;padding:20px}
.step-help .eyebrow{margin-bottom:6px}
.step-help h2{font-size:15px;margin:0 0 8px}
.step-help p{font-size:12px;color:var(--muted);line-height:1.8;margin:0 0 10px}
.step-help ul{margin:0 0 14px;padding-left:17px;color:#52525b;font-size:12px;line-height:1.85}
.step-help .help-link{font-size:12px;font-weight:600}
.context-help{display:block;background:#f7f9fc;border:1px solid #dde6f2;border-radius:12px;margin:0 0 16px;font-size:12.5px;color:#4b5563;line-height:1.75;overflow:hidden}
.context-help summary{display:flex;align-items:flex-start;gap:12px;padding:12px 14px;cursor:pointer;list-style:none}
.context-help summary::-webkit-details-marker{display:none}
.context-help summary:hover{background:#f2f6fc}
.context-help .context-help-icon{width:24px;height:24px;border-radius:7px;background:#e8eef7;color:var(--blue);display:grid;place-items:center;font-size:12px;font-weight:700;flex:none;margin-top:1px}
.context-help .context-help-main{min-width:0;flex:1}
.context-help strong{color:#334155;display:block;margin-bottom:2px}
.context-help .context-help-peek{display:block;color:#64748b;font-size:12px;line-height:1.65}
.context-help .context-help-toggle{flex:none;color:var(--blue);font-weight:600;font-size:0;white-space:nowrap;margin-top:1px}
.context-help .context-help-toggle::before{content:"展开要点";font-size:12px}
.context-help[open] .context-help-toggle::before{content:"收起"}
.context-help-body{padding:0 14px 14px 50px;border-top:1px solid #e6edf7;margin-top:2px}
.context-help-body ul{margin:10px 0 8px;padding-left:17px;color:#4b5563;font-size:12.5px;line-height:1.8}
.context-help-body li{margin:4px 0}
.context-help-body a{font-weight:600;font-size:12px}
.help-back-row{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:10px}
.help-back-row .back-link{margin-bottom:0}
.help-back-row .text-link{font-size:12px;color:var(--muted)}
.getting-started{background:#fff;border:1px solid var(--line);border-radius:16px;padding:20px;margin:0 0 20px}
.getting-started-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap}
.getting-started-head h2{font-size:17px;margin:0 0 4px}
.getting-started-head p{font-size:12px;color:var(--muted);margin:0}
.getting-started-actions{display:flex;gap:12px;align-items:center;font-size:12px}
.getting-started-steps{list-style:none;display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin:18px 0 0;padding:16px 0 0;border-top:1px solid #edf0f4}
.getting-started-steps li{position:relative;display:flex;flex-direction:column;gap:5px;font-size:11px;color:var(--muted);line-height:1.5}
.getting-started-steps b{width:24px;height:24px;border-radius:8px;background:#eef4fc;color:var(--blue);display:grid;place-items:center;font-size:11px}
.getting-started-steps strong{display:block;color:#3f3f46;font-size:12px;font-weight:600}
.help-toolbar{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;margin:0 0 16px}
.help-search{max-width:360px;width:100%;padding:10px 12px;border:1px solid #cbd2dc;border-radius:9px;background:#fff}
.help-count{font-size:12px;color:var(--muted);white-space:nowrap}
.help-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;align-items:start}
.help-card{background:#fff;border:1px solid var(--line);border-radius:15px;overflow:hidden;scroll-margin-top:84px}
.help-card[open]{grid-column:1/-1;border-color:#cbd8ea;box-shadow:0 10px 30px rgba(29,29,31,.06)}
.help-card summary{list-style:none;cursor:pointer;padding:20px;display:grid;grid-template-columns:38px minmax(0,1fr);gap:12px;align-items:start}
.help-card summary::-webkit-details-marker{display:none}
.help-card summary:hover{background:#fafbfc}
.help-card-icon{width:38px;height:38px;border-radius:11px;background:#eef4fc;color:var(--blue);display:grid;place-items:center;font-size:12px;font-weight:700}
.help-card-title{font-size:16px;font-weight:650;margin:1px 0 4px;color:var(--ink)}
.help-card-summary{font-size:12px;line-height:1.7;color:var(--muted);margin:0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.help-card-open{display:inline-block;margin-top:9px;font-size:11px;color:var(--blue);font-weight:600}
.help-card-body{padding:0 20px 22px 70px;border-top:1px solid #edf0f4}
.help-card-body h3{font-size:13px;margin:18px 0 8px;color:#3f3f46}
.help-card-body p,.help-card-body li{font-size:12.5px;line-height:1.85;color:#52525b}
.help-card-body ol,.help-card-body ul{padding-left:18px;margin:8px 0}
.help-card-body .help-flow{margin:18px 0 4px;padding:16px;border:1px solid #e3e9f2;border-radius:12px;background:linear-gradient(135deg,#fff,#f7f9fc)}
.help-flow-title{display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:11px;color:var(--muted);margin-bottom:12px}
.help-flow-steps{list-style:none;display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px;margin:0;padding:0}
.help-flow-steps li{position:relative;display:flex;flex-direction:column;gap:5px;font-size:10.5px;line-height:1.45;color:#52525b}
.help-flow-steps b{width:22px;height:22px;border-radius:7px;background:#fff;border:1px solid #dce5f1;color:var(--blue);display:grid;place-items:center;font-size:10px}
.help-problems{display:grid;gap:10px;margin-top:6px}
.help-problem{background:#f8f9fb;border:1px solid #edf0f4;border-radius:9px;padding:10px 12px}
.help-problem strong{display:block;font-size:12px;color:#3f3f46;margin-bottom:3px}
.help-problem p{font-size:12px;margin:0}
.help-empty{padding:34px;text-align:center;color:var(--muted);background:#fff;border:1px solid var(--line);border-radius:14px}
.file-upload-box{background:#f8f9fb;border:1px dashed #cbd5e1;border-radius:12px;padding:18px}
.file-upload-box h2{font-size:15px;margin:0 0 4px}
.file-upload-box p{font-size:12px;color:var(--muted);margin:0 0 12px}
.file-input{width:100%;padding:10px;border:1px solid #cbd2dc;border-radius:9px;background:#fff;font-size:12px}
.file-list{display:flex;flex-direction:column;gap:8px;margin-top:14px}
.file-row{display:grid;grid-template-columns:minmax(0,1fr) 120px 90px;gap:14px;align-items:center;background:#fff;border:1px solid var(--line);border-radius:11px;padding:12px 14px}
.file-row .file-name{font-size:13px;font-weight:600;color:#3f3f46;overflow-wrap:anywhere}
.file-row .file-meta{font-size:11px;color:var(--muted);margin-top:3px;overflow-wrap:anywhere}
.file-row .file-size{font-size:11px;color:var(--muted);text-align:right;white-space:nowrap}
.file-row .file-actions{display:flex;gap:10px;justify-content:flex-end;font-size:11px}
.file-kind{display:inline-block;padding:2px 7px;border-radius:999px;background:#eef4fc;color:#1d4ed8;font-size:10px;font-weight:600;margin-right:6px}
.file-kind--template{background:#ecfdf5;color:var(--teal)}
.file-kind--reference{background:#fff7e6;color:var(--warn)}
.file-empty{padding:24px;text-align:center;color:var(--muted);font-size:12px;background:#fff;border:1px dashed #d7dee8;border-radius:11px;margin-top:12px}

/* 使用帮助 */
.help-layout{display:grid;grid-template-columns:210px minmax(0,1fr);gap:34px;align-items:start}
.help-toc{position:sticky;top:86px;background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px}
.help-toc strong{display:block;font-size:12px;color:#3f3f46;margin-bottom:9px}
.help-toc a{display:block;padding:6px 0;font-size:12px;color:var(--muted)}
.help-toc a:hover{color:var(--blue)}
.help-body section{background:#fff;border:1px solid var(--line);border-radius:16px;padding:24px;margin-bottom:14px;scroll-margin-top:84px}
.help-body h2{font-size:19px;margin:0 0 12px}
.help-body h3{font-size:14px;margin:18px 0 6px}
.help-body p,.help-body li{color:#52525b;font-size:13px;line-height:1.85}
.help-body ul,.help-body ol{padding-left:20px;margin:8px 0}
.help-body .callout{background:#f8f9fb;border:1px solid #edf0f4;border-radius:10px;padding:12px 14px;font-size:12px;color:#52525b;line-height:1.8;margin-top:12px}

/* 内部示例 */
.internal-body{background:#f3f5f8;color:#1f2937}
.internal-wrap{max-width:1100px;margin:0 auto;padding:28px 22px 50px}
.internal-card{background:#fff;border:1px solid #dbe2ea;border-radius:10px;padding:20px;margin:16px 0;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.internal-card h1,.internal-card h2{color:#1f3864}
.internal-table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px}
.internal-table th,.internal-table td{border:1px solid #d7dee8;padding:6px 8px;text-align:left}
.internal-table th{background:#1f3864;color:#fff}
.internal-table th:nth-child(-n+6),.internal-table td:nth-child(-n+6){white-space:nowrap}
.internal-table td:last-child{word-break:break-all;min-width:280px}
.internal-table td.num{text-align:right;font-variant-numeric:tabular-nums}
.internal-error{background:#fef2f2;border:1px solid #fecaca;color:#991b1b;padding:10px 13px;border-radius:6px;white-space:pre-wrap}

@media(max-width:980px){
  .auth-grid{grid-template-columns:1fr;gap:30px;padding:36px 0 42px;min-height:0}
  .auth-story .proof-list{grid-template-columns:1fr 1fr;display:grid}
  .auth-card{max-width:560px}
  .quote-summary{grid-template-columns:1fr}
  .quote-row{grid-template-columns:minmax(0,1fr) 130px}
  .quote-actions{grid-column:1/-1;justify-content:flex-start;padding-top:10px;border-top:1px solid #edf0f4}
  .help-layout{grid-template-columns:1fr}
  .help-toc{position:static;display:flex;flex-wrap:wrap;gap:4px 16px;align-items:center}
  .help-toc a{padding:4px 0}
  .step-layout{grid-template-columns:1fr}
  .step-help{position:static}
  .getting-started-steps{grid-template-columns:repeat(3,minmax(0,1fr))}
  .help-flow-steps{grid-template-columns:repeat(3,minmax(0,1fr))}
}
@media(max-width:860px){
  .app-shell{display:block}
  .sidebar{width:100%;height:auto;position:static;display:block}
  .sidebar-brand{padding:15px 18px 10px}
  .side-nav{flex-direction:row;padding:6px 12px 10px;border-top:0;border-bottom:1px solid #262b36}
  .side-nav a{padding:8px 12px}
  .sidebar-project,.sidebar-foot{display:none}
  .appbar{padding:0 18px;min-height:52px}
  .content{padding:22px 18px 40px}
  .content-footer{padding:18px;flex-wrap:wrap}
  .content-footer .product{margin-left:0;width:100%}
  .stat-row{grid-template-columns:1fr}
  .project-grid{grid-template-columns:1fr}
  .workflow-steps{grid-template-columns:repeat(2,minmax(0,1fr))}
  .recon-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .guide-grid{grid-template-columns:1fr}
  .landing-flow{grid-template-columns:1fr}
  .landing-trust{grid-template-columns:1fr;margin-top:38px}
  .help-grid{grid-template-columns:1fr}
  .help-card[open]{grid-column:auto}
  .help-card-body{padding-left:20px}
  .file-row{grid-template-columns:minmax(0,1fr) 90px}
  .file-row .file-size{text-align:left}
  .file-row .file-actions{grid-column:1/-1;justify-content:flex-start;padding-top:8px;border-top:1px solid #edf0f4}
  .site-foot{flex-wrap:wrap}
  .site-foot .product{margin-left:0;width:100%}
}
@media(max-width:620px){
  .public-header{padding:18px 18px 0;align-items:flex-start}
  .public-nav{gap:10px}
  .identity-text{font-size:15px}
  .identity-text small{font-size:10px}
  .brand-mark{width:32px;height:32px;border-radius:9px;font-size:10px}
  .landing-hero{padding:58px 18px 28px}
  .landing-hero h1{font-size:clamp(34px,10vw,48px)}
  .landing-actions{gap:12px}
  .auth-shell{padding:0 18px}
  .auth-story .proof-list{grid-template-columns:1fr}
  .auth-card{padding:26px 22px}
  .form-grid{grid-template-columns:1fr}
  .kpis{grid-template-columns:1fr}
  .page-head{align-items:flex-start}
  .page-actions{width:100%}
  .page-actions .btn{flex:1}
  .quote-row{grid-template-columns:1fr;gap:8px}
  .quote-total{text-align:left}
  .quote-actions{justify-content:flex-start}
  .quote-head{align-items:flex-start}
  .appbar-actions .help-link{display:none}
  .user-name{display:none}
  .workflow-steps{grid-template-columns:1fr}
  .recon-grid{grid-template-columns:1fr}
  .getting-started-steps{grid-template-columns:repeat(2,minmax(0,1fr))}
  .help-flow-steps{grid-template-columns:repeat(2,minmax(0,1fr))}
  .getting-started-head{align-items:flex-start}
  .getting-started-actions{width:100%}
}
@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.001ms !important;animation-iteration-count:1 !important;transition-duration:.001ms !important;scroll-behavior:auto !important}
}
"""

APP_JS = """
(function(){
  const overlay=document.getElementById('loading-overlay');
  const message=document.getElementById('loading-message');
  const loadingNote=document.getElementById('loading-note');
  const loadingElapsed=document.getElementById('loading-elapsed');
  let loadingHintTimer=null;
  let loadingClockTimer=null;
  function show(text){
    if(!overlay)return;
    if(message)message.textContent=text||'正在处理…';
    if(loadingNote){
      const hints=[
        '正在校验文件与权限…',
        '正在读取图纸或模板…',
        '正在计算工程量与金额…',
        '正在生成 Excel、PDF 和预览…',
        '马上就好，请保持页面打开…'
      ];
      let index=0;
      const started=Date.now();
      loadingNote.textContent=hints[0];
      if(loadingElapsed)loadingElapsed.textContent='已用时 0 秒';
      if(loadingHintTimer)clearInterval(loadingHintTimer);
      if(loadingClockTimer)clearInterval(loadingClockTimer);
      loadingHintTimer=setInterval(function(){
        index=(index+1)%hints.length;
        loadingNote.textContent=hints[index];
      },2400);
      loadingClockTimer=setInterval(function(){
        if(loadingElapsed){
          loadingElapsed.textContent='已用时 '+Math.max(1,Math.floor((Date.now()-started)/1000))+' 秒';
        }
      },1000);
    }
    overlay.hidden=false;
    document.body.setAttribute('aria-busy','true');
  }
  window.addEventListener('pageshow',function(event){
    // 浏览器“后退”可能从 bfcache/内存缓存恢复旧页面，导致资料/检查/价格状态过期。
    // 状态页重新向服务器取一次；纯录入表单不强制刷新，避免丢输入。
    let restored=!!event.persisted;
    try{
      const nav=performance.getEntriesByType('navigation')[0];
      restored=restored||!!(nav&&nav.type==='back_forward');
    }catch(error){}
    if(restored && document.querySelector('.workflow,.file-row,.quote-row,.quote-summary,.steps,.kpi')){
      window.location.reload();
      return;
    }
    if(overlay)overlay.hidden=true;
    document.body.removeAttribute('aria-busy');
    if(loadingHintTimer)clearInterval(loadingHintTimer);
    if(loadingClockTimer)clearInterval(loadingClockTimer);
  });
  document.querySelectorAll('form[data-loading]').forEach(function(form){
    form.addEventListener('submit',function(){
      const button=form.querySelector('button[type=submit]');
      if(button){button.disabled=true;}
      show(form.dataset.loading||'正在处理…');
    });
  });
  document.querySelectorAll('[data-history-back]').forEach(function(link){
    link.addEventListener('click',function(event){
      try{
        if(document.referrer && new URL(document.referrer).origin===location.origin && history.length>1){
          event.preventDefault();
          history.back();
        }
      }catch(error){}
    });
  });
  document.querySelectorAll('form[data-confirm]').forEach(function(form){
    form.addEventListener('submit',function(event){
      if(!window.confirm(form.dataset.confirm))event.preventDefault();
    });
  });
  document.querySelectorAll('[data-transition]').forEach(function(link){
    link.addEventListener('click',function(event){
      if(window.matchMedia('(prefers-reduced-motion: reduce)').matches)return;
      const href=link.getAttribute('href');
      if(!href)return;
      event.preventDefault();
      show(link.dataset.transition||'正在准备…');
      setTimeout(function(){window.location.href=href;},420);
    });
  });
  const deleteForm=document.getElementById('delete-project-form');
  if(deleteForm){
    const check=document.getElementById('delete-confirm');
    const button=document.getElementById('delete-submit');
    if(check&&button){
      check.addEventListener('change',function(){button.disabled=!check.checked;});
      button.disabled=!check.checked;
    }
  }
  const filter=document.getElementById('project-filter');
  if(filter){
    const cards=Array.from(document.querySelectorAll('[data-project-name]'));
    filter.addEventListener('input',function(){
      const needle=filter.value.trim().toLowerCase();
      cards.forEach(function(card){
        card.hidden=Boolean(needle)&&!card.dataset.projectName.toLowerCase().includes(needle);
      });
    });
  }
  const loginForm=document.querySelector('form[data-login]');
  if(loginForm){
    loginForm.addEventListener('submit',function(){
      const button=loginForm.querySelector('button[type=submit]');
      if(button){button.disabled=true;button.textContent=button.dataset.busy||'正在登录…';}
    });
  }
  const helpFilter=document.getElementById('help-filter');
  if(helpFilter){
    const cards=Array.from(document.querySelectorAll('[data-help-card]'));
    const count=document.getElementById('help-count');
    const empty=document.getElementById('help-empty');
    function filterHelp(){
      const needle=helpFilter.value.trim().toLowerCase();
      let visible=0;
      cards.forEach(function(card){
        const text=(card.dataset.keywords+' '+card.textContent).toLowerCase();
        const show=!needle||text.includes(needle);
        card.hidden=!show;
        if(show)visible+=1;
      });
      if(count)count.textContent=visible+' / '+cards.length+' 篇';
      if(empty)empty.hidden=visible!==0;
    }
    helpFilter.addEventListener('input',filterHelp);
    filterHelp();
  }
  document.querySelectorAll('.help-card').forEach(function(card){
    const label=card.querySelector('.help-card-open');
    card.addEventListener('toggle',function(){
      if(label)label.textContent=card.open?'收起步骤':'展开步骤';
    });
  });
  if(location.hash.length>1){
    try{
      const target=document.querySelector(location.hash);
      if(target&&target.tagName==='DETAILS'){
        target.open=true;
        target.scrollIntoView({block:'start'});
      }
    }catch(error){}
  }
  const draftScale=document.getElementById('draft-scale');
  if(draftScale){
    draftScale.addEventListener('input',function(){
      const scale=Number(draftScale.value);
      if(!Number.isFinite(scale)||scale<=0)return;
      document.querySelectorAll('.route-length').forEach(function(input){
        const raw=Number(input.dataset.raw);
        if(Number.isFinite(raw)&&raw>0){
          input.value=(raw*scale).toFixed(6).replace(/0+$/,'').replace(/[.]$/,'');
        }
      });
    });
  }
  const gettingStarted=document.getElementById('getting-started-card');
  if(gettingStarted){
    const storageKey='hvac.gettingStarted.collapsed.v1';
    if(sessionStorage.getItem(storageKey)==='1')gettingStarted.hidden=true;
    document.querySelectorAll('[data-collapse-getting-started]').forEach(function(button){
      button.addEventListener('click',function(){
        sessionStorage.setItem(storageKey,'1');
        gettingStarted.hidden=true;
      });
    });
  }
})();
"""


def esc(value) -> str:
    return escape("" if value is None else str(value))


def money(value) -> str:
    try:
        return f"{Decimal(str(value)):,.2f}"
    except (InvalidOperation, TypeError, ValueError):
        return esc(value)


def quantity(value) -> str:
    return esc(format_quantity(value))


def date_text(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return value


def datetime_text(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return value


def brand_mark() -> str:
    return '<span class="brand-mark" aria-hidden="true">HQ</span>'




def company_line() -> str:
    return '<span class="company"><strong>HVAC Quote</strong></span>'


def document(title: str, body: str, body_class: str = "") -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<style>{STYLE}</style></head>
<body class="{esc(body_class)}">
{body}
</body></html>"""


def overlay() -> str:
    return """
<div id="loading-overlay" class="loading-overlay" hidden aria-live="polite">
  <div class="loading-card" role="status">
    <p class="loading-eyebrow">HQ · 正在处理</p>
    <h2 id="loading-message">正在处理…</h2>
    <p id="loading-note" class="loading-note">正在校验文件与权限…</p>
    <div class="loading-track" aria-hidden="true"><span></span></div>
    <div class="loading-meta"><span id="loading-elapsed">已用时 0 秒</span><span>请勿关闭页面</span></div>
  </div>
</div>"""


def _public_header(user: dict | None, *, show_nav: bool = True) -> str:
    if not show_nav:
        return f"""
<header class="public-header">
  <a class="identity" href="/">{brand_mark()}
    <span class="identity-text">HVAC Quote<small>工程报价工作台</small></span></a>
</header>"""
    if user:
        action = '<a class="btn btn--small" href="/projects">项目总览</a>'
    else:
        action = '<a class="btn btn--small" href="/login">登录工作台</a>'
    return f"""
<header class="public-header">
  <a class="identity" href="/">{brand_mark()}
    <span class="identity-text">HVAC Quote<small>工程报价工作台</small></span></a>
  <nav class="public-nav" aria-label="主导航">
    <a class="text-link" href="/help">使用帮助</a>
    {action}
  </nav>
</header>"""


def _site_footer() -> str:
    return f"""
<footer class="site-foot">
  {company_line()}
  <p class="slogan">从图纸检查到报价交付</p>
  <span class="product">HVAC Quote 工程报价</span>
</footer>"""


def _workflow_flow_html() -> str:
    steps = "".join(
        f"<li><b>{index:02d}</b><strong>{esc(title)}</strong>"
        f"<span>{esc(detail)}</span></li>"
        for index, (title, detail) in enumerate(WORKFLOW_STEPS, start=1)
    )
    return f"""
<div class="help-flow">
  <div class="help-flow-title"><span>一份报价的完整路径</span><span>六步</span></div>
  <ol class="help-flow-steps">{steps}</ol>
</div>"""


def getting_started_card() -> str:
    steps = "".join(
        f"<li><b>{index:02d}</b><strong>{esc(title)}</strong>"
        f"<span>{esc(detail)}</span></li>"
        for index, (title, detail) in enumerate(WORKFLOW_STEPS, start=1)
    )
    return f"""
<section id="getting-started-card" class="getting-started" aria-label="首次使用引导">
  <div class="getting-started-head">
    <div>
      <h2>第一次使用？从这里开始。</h2>
      <p>不用先读完整说明，按六步走；每一步都可以单独回来查看。</p>
    </div>
    <div class="getting-started-actions">
      <a class="text-link" href="/help">查看图解说明</a>
      <button class="text-button" type="button" data-collapse-getting-started>暂时收起</button>
    </div>
  </div>
  <ol class="getting-started-steps">{steps}</ol>
</section>"""


def _help_points(text: str) -> list[str]:
    parts = [
        part.strip(" 。；;")
        for part in re.split(r"[；。]\s*", text or "")
        if part.strip(" 。；;")
    ]
    if len(parts) <= 1:
        parts = [
            part.strip()
            for part in re.split(r"\s*→\s*", text or "")
            if part.strip()
        ]
    return parts[:5]


def _context_help(title: str, text: str, href: str, link_text: str) -> str:
    points = _help_points(text)
    preview = points[0] if points else text
    body_points = points[1:] if len(points) > 1 else points
    items = "".join(f"<li>{esc(point)}</li>" for point in body_points)
    return f"""
<details class="context-help">
  <summary>
    <span class="context-help-icon" aria-hidden="true">?</span>
    <span class="context-help-main"><strong>{esc(title)}</strong>
      <span class="context-help-peek">{esc(preview)}</span></span>
    <span class="context-help-toggle" aria-hidden="true"></span>
  </summary>
  <div class="context-help-body">
    <ul>{items}</ul>
    <a href="{esc(href)}">{esc(link_text)}</a>
  </div>
</details>"""


def _file_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def public_page(title: str, body: str, user: dict | None = None) -> str:
    content = f"""{_public_header(user)}
<main class="wrap">{body}</main>
{_site_footer()}
{overlay()}
<script>{APP_JS}</script>"""
    return document(title, content)


def _sidebar(active: str, user: dict, project: Project | None) -> str:
    nav = [
        ("/projects", "项目总览", "▦", "projects"),
        ("/customers", "客户", "◉", "customers"),
        ("/account", "账号", "◐", "account"),
        ("/help", "使用帮助", "?", "help"),
    ]
    links = "".join(
        f'<a href="{href}" class="{"active" if key == active else ""}">'
        f'<span class="nav-icon" aria-hidden="true">{icon}</span>{label}</a>'
        for href, label, icon, key in nav
    )
    project_block = ""
    if project is not None:
        project_block = (
            '<div class="sidebar-project"><span>当前项目</span>'
            f"<strong>{esc(project.name)}</strong></div>"
        )
    return f"""
<aside class="sidebar">
  <a class="sidebar-brand" href="/projects">{brand_mark()}
    <span><strong>HVAC Quote</strong><small>工程报价工作台</small></span></a>
  <nav class="side-nav" aria-label="主导航">{links}</nav>
  {project_block}
  <div class="sidebar-foot">
    <div class="company"><span>HVAC Quote</span></div>
    <p>从图纸检查到报价交付</p>
    <span class="product">HVAC Quote 工程报价</span>
  </div>
</aside>"""


def account_page(
    user: dict,
    *,
    error: str | None = None,
    changed: bool = False,
    default_template: dict | None = None,
    vision_enabled: bool = False,
) -> str:
    display_name = str(user.get("displayName") or user.get("username") or "")
    notice = (
        '<div class="notice notice--success" role="status">密码已修改，下次登录请使用新密码。</div>'
        if changed
        else ""
    )
    error_html = f'<div class="alert" role="alert">{esc(error)}</div>' if error else ""
    admin_link = ""
    if user.get("isAdmin"):
        admin_link = (
            '<div class="card"><h2>账号管理</h2>'
            '<p class="muted" style="margin:6px 0 14px">'
            "新增账号、重置其他账号的密码。</p>"
            '<a class="btn" href="/admin/users">进入账号管理</a></div>'
        )
    if default_template:
        template_card = (
            '<div class="card"><h2>账号默认报价模板</h2>'
            f'<p class="muted" style="margin:6px 0 8px">当前：'
            f'{esc(default_template.get("priceBookName") or default_template.get("fileName") or "")}</p>'
            f'<p class="muted" style="margin:0 0 8px">文件：'
            f'{esc(default_template.get("fileName") or "")} · '
            f'SHA256 {esc(str(default_template.get("sha256") or "")[:16])}…</p>'
            '<p class="muted" style="margin:0 0 14px">新建项目会自动准备这份受保护模板和价格书；'
            "更换入口在项目资料的模板价格书页。删除原项目不会删除这里的副本。</p>"
            '<form method="post" action="/account/default-template/clear" '
            'data-confirm="清除账号默认模板？以后新项目需要重新上传或选择模板。">'
            '<button class="btn btn--small btn--ghost" type="submit">清除默认模板</button></form>'
            "</div>"
        )
    else:
        template_card = (
            '<div class="card"><h2>账号默认报价模板</h2>'
            '<p class="muted" style="margin:6px 0 0">还没有设置。第一次成功导入客户模板后会自动记住；'
            "也可以在项目资料的模板价格书页点“设为账号默认”。</p></div>"
        )
    vision_card = f"""
<div class="card">
  <h2>视觉辅助（试验）</h2>
  <p class="muted" style="margin:6px 0 14px">
    当前：{'已开启' if vision_enabled else '已关闭'}。开启后仍需在草稿页选中具体疑难局部并预览，
    再主动提交；不会自动发送整图，也不会自动改量或定价。
  </p>
  <form method="post" action="/account/vision-preference">
    <input type="hidden" name="enabled" value="{'0' if vision_enabled else '1'}">
    <button class="btn btn--small {'btn--ghost' if vision_enabled else ''}" type="submit">
      {'关闭视觉辅助' if vision_enabled else '开启视觉辅助'}
    </button>
  </form>
</div>"""
    body = f"""
{notice}
<div class="card">
  <h2>修改密码</h2>
  <p class="muted" style="margin:6px 0 14px">当前账号：{esc(display_name)}（{esc(str(user.get("username") or ""))}）</p>
  {error_html}
  <form method="post" action="/account" data-loading="正在修改密码…">
    <div class="form-grid">
      <label class="field"><span>当前密码</span>
        <input name="current_password" type="password" autocomplete="current-password" maxlength="128" placeholder="输入当前密码"></label>
      <label class="field"><span>新密码（至少 8 位）</span>
        <input name="new_password" type="password" autocomplete="new-password" maxlength="128" placeholder="输入新密码"></label>
      <label class="field"><span>确认新密码</span>
        <input name="password_confirm" type="password" autocomplete="new-password" maxlength="128" placeholder="再输入一次新密码"></label>
    </div>
    <div class="form-actions"><button class="btn" type="submit">保存新密码</button></div>
  </form>
</div>
{vision_card}
{template_card}
{admin_link}"""
    return app_page(
        "账号设置 · HVAC Quote 工程报价",
        body,
        user,
        active="account",
        crumbs=_crumb((("账号设置", None),)),
    )


def admin_users_page(
    user: dict,
    users: list[dict],
    *,
    error: str | None = None,
    created: str = "",
    reset: str = "",
    values: dict | None = None,
) -> str:
    notice = ""
    if created:
        notice = (
            '<div class="notice notice--success" role="status">'
            f"已创建账号 {esc(created)}，对方现在可以用它登录。</div>"
        )
    elif reset:
        notice = (
            '<div class="notice notice--success" role="status">'
            f"已重置 {esc(reset)} 的密码。</div>"
        )
    error_html = f'<div class="alert" role="alert">{esc(error)}</div>' if error else ""
    form_values = values or {}
    rows = []
    for item in users:
        role = "管理员" if item.get("isAdmin") else "成员"
        rows.append(
            "<tr>"
            f"<td>{esc(str(item.get('displayName') or ''))}</td>"
            f"<td>{esc(str(item.get('username') or ''))}</td>"
            f"<td>{esc(str(item.get('createdAt') or ''))}</td>"
            f"<td>{role}</td>"
            "<td>"
            f'<form method="post" action="/admin/users/{esc(str(item.get("username") or ""))}/password" data-loading="正在重置密码…">'
            '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
            '<input name="new_password" type="password" maxlength="128" placeholder="新密码" style="width:130px;padding:8px 10px;border:1px solid #cbd2dc;border-radius:8px">'
            '<input name="password_confirm" type="password" maxlength="128" placeholder="确认" style="width:100px;padding:8px 10px;border:1px solid #cbd2dc;border-radius:8px">'
            '<button class="text-button" type="submit">重置</button>'
            "</div></form></td>"
            "</tr>"
        )
    body = f"""
{_history_back("返回账号设置", "/account")}
{notice}
{error_html}
<div class="card">
  <h2>账号管理</h2>
  <p class="muted" style="margin:6px 0 14px">第一个创建的账号是管理员；只有管理员可以进入本页。</p>
  <div class="table-wrap"><table class="table">
    <thead><tr><th>显示名称</th><th>用户名</th><th>创建时间</th><th>角色</th><th>重置密码</th></tr></thead>
    <tbody>{''.join(rows) or '<tr><td colspan="5">还没有账号</td></tr>'}</tbody>
  </table></div>
</div>
<div class="card">
  <h2>新增账号</h2>
  <form method="post" action="/admin/users" data-loading="正在创建账号…">
    <div class="form-grid">
      <label class="field"><span>用户名（2-32 位字母、数字、点、下划线或短横线）</span>
        <input name="username" maxlength="32" value="{esc(str(form_values.get('username') or ''))}" placeholder="例如 zhangsan"></label>
      <label class="field"><span>显示名称</span>
        <input name="display_name" maxlength="40" value="{esc(str(form_values.get('display_name') or ''))}" placeholder="例如 张工"></label>
      <label class="field"><span>密码（至少 8 位）</span>
        <input name="password" type="password" autocomplete="new-password" maxlength="128" placeholder="输入初始密码"></label>
      <label class="field"><span>确认密码</span>
        <input name="password_confirm" type="password" autocomplete="new-password" maxlength="128" placeholder="再输入一次"></label>
    </div>
    <div class="form-actions"><button class="btn" type="submit">创建账号</button></div>
  </form>
</div>"""
    return app_page(
        "账号管理 · HVAC Quote 工程报价",
        body,
        user,
        active="account",
        crumbs=_crumb((("账号设置", "/account"), ("账号管理", None))),
    )



def preflight_page(
    user: dict,
    project: Project,
    item,
    report: dict,
) -> str:
    views = report.get("views") or []
    rows = []
    for view in views:
        opaque = view.get("opaqueBlocks") or []
        status = (
            '<span class="badge badge--ok">预算内</span>'
            if view.get("withinBudget")
            else '<span class="badge badge--warn">可能超限</span>'
        )
        rows.append(
            "<tr>"
            f'<td><label class="check-line"><input type="checkbox" '
            f'name="views" value="{esc(str(view.get("view") or ""))}"> '
            f'{esc(str(view.get("view") or ""))}</label></td>'
            f'<td class="num">{int(view.get("rootCount") or 0)}</td>'
            f'<td class="num">{int(view.get("estimatedEntities") or 0)}</td>'
            f'<td class="num">{int(view.get("effectiveEntities") or 0)}</td>'
            f"<td>{status}</td>"
            f'<td class="num">{len(opaque)}</td>'
            "</tr>"
        )
    opaque_summary = []
    for view in views:
        for block in (view.get("opaqueBlocks") or [])[:5]:
            opaque_summary.append(
                f"{esc(str(view.get('view') or ''))} · "
                f"{esc(str(block.get('blockName') or ''))}："
                f"{int(block.get('instances') or 0)} 个实例 × "
                f"{int(block.get('childCount') or 0)} 子图元"
            )
    body = f"""
{_history_back("返回项目资料", f"/projects/{project.id}/files")}
<div class="page-head">
  <div>
    <p class="eyebrow">{esc(project.name)}</p>
    <h1>图纸预检：{esc(item.original_name)}</h1>
    <p class="lead">预检只统计布局、根对象和图块展开规模，不展开全部几何。请选择本次要检查的布局，再开始精读。</p>
  </div>
</div>
<div class="notice notice--info">
  全图估算 {int(report.get("totalEstimated") or 0):,} 个图元；
  折叠无暖通证据的大图块后约 {int(report.get("totalEffective") or 0):,} 个，
  实体预算 {int(report.get("maxEntities") or 0):,}。
  超过预算时不会静默截断，只会明确失败或按所选范围检查。
</div>
<div class="card">
  <h2>布局与规模</h2>
  <div class="table-wrap"><table class="table">
    <thead><tr><th>本次范围</th><th>根对象</th><th>估算图元</th><th>折叠后估算</th><th>预算</th><th>折叠大图块</th></tr></thead>
    <tbody>{''.join(rows) or '<tr><td colspan="6">没有可读取的布局</td></tr>'}</tbody>
  </table></div>
  <p class="muted">勾选一个或多个布局后点“按所选范围检查”；不勾选则检查全图。</p>
  <form method="post" action="/projects/{esc(project.id)}/files/{esc(item.id)}/inspect" data-loading="正在按所选范围检查…">
    <div class="form-actions">
      <button class="btn" type="submit">按勾选范围检查</button>
    </div>
  </form>
  <form method="post" action="/projects/{esc(project.id)}/files/{esc(item.id)}/inspect" data-loading="正在检查全图…" style="margin-top:10px">
    <div class="form-actions">
      <button class="btn btn--ghost" type="submit">检查全图（范围未勾选时）</button>
    </div>
  </form>
</div>
<div class="card">
  <h2>折叠的上下文图块</h2>
  <p class="muted">这些图块没有发现暖通/管线证据，按一个上下文记录处理；展开量、实例数和块名保留在诊断中，可审计。</p>
  <ul class="excluded">{''.join(f"<li>{line}</li>" for line in opaque_summary) or '<li>没有需要折叠的大图块。</li>'}</ul>
</div>
"""
    return app_page(
        "图纸预检 · HVAC Quote 工程报价",
        body,
        user,
        active="projects",
        project=project,
        crumbs=_crumb(
            (
                ("我的项目", "/projects"),
                (project.name, f"/projects/{project.id}"),
                ("图纸预检", None),
            )
        ),
    )


def app_page(
    title: str,
    body: str,
    user: dict,
    *,
    active: str = "projects",
    crumbs: str = "",
    project: Project | None = None,
) -> str:
    display_name = str(user.get("displayName") or user.get("username") or "")
    initial = display_name[:1] if display_name else "T"
    content = f"""<div class="app-shell">
{_sidebar(active, user, project)}
<div class="app-main">
  <header class="appbar">
    <div class="crumbs">{crumbs}</div>
    <div class="appbar-actions">
      <a class="help-link" href="/help">使用帮助</a>
      <span class="user-chip"><span class="user-avatar" aria-hidden="true">{esc(initial)}</span>
        <span class="user-name">{esc(display_name)}</span></span>
      <form method="post" action="/logout"><button class="text-button" type="submit">退出</button></form>
    </div>
  </header>
  <main class="content">{body}</main>
  <footer class="content-footer">
    {company_line()}
    <p class="slogan">从图纸检查到报价交付</p>
    <span class="product">本报价按本次确认范围生成</span>
  </footer>
</div>
</div>
{overlay()}
<script>{APP_JS}</script>"""
    return document(title, content, "app-body")


def _history_back(label: str, fallback: str) -> str:
    return (
        f'<a class="back-link" href="{esc(fallback)}" data-history-back>'
        f"← {esc(label)}</a>"
    )


def _crumb(items: tuple[tuple[str, str | None], ...]) -> str:
    parts = []
    for index, (label, href) in enumerate(items):
        if index:
            parts.append('<span class="sep" aria-hidden="true">/</span>')
        if href:
            parts.append(f'<a href="{esc(href)}">{esc(label)}</a>')
        else:
            parts.append(f'<span class="current">{esc(label)}</span>')
    return "".join(parts)


def landing_page(
    user: dict | None = None, *, setup_available: bool = False
) -> str:
    if user:
        primary = '<a class="btn landing-cta" href="/projects" data-transition="正在进入项目总览…">进入项目总览 <span aria-hidden="true">→</span></a>'
    elif setup_available:
        primary = '<a class="btn landing-cta" href="/setup" data-transition="正在准备首次设置…">创建管理员账号 <span aria-hidden="true">→</span></a>'
    else:
        primary = '<a class="btn landing-cta" href="/login" data-transition="正在准备登录…">登录并开始 <span aria-hidden="true">→</span></a>'
    body = f"""
<div class="landing">
  <div class="landing-orbit" aria-hidden="true"></div>
  <section class="landing-hero">
    <p class="eyebrow">面向长期项目的工程报价工作台</p>
    <h1>让每一份报价，<br><span class="accent">都有据可查。</span></h1>
    <p class="lead">从项目开始，核对图纸、补充缺失信息，生成可交付的 Excel 与 PDF。
    客户、楼层、报价版本和补充记录，都留在同一个地方。</p>
    <div class="landing-actions">
      {primary}
      <a class="text-link" href="/help">先看看怎么用</a>
    </div>
    <div class="landing-trust">
      <div><strong>项目归档</strong><span>一个客户，多份报价</span></div>
      <div><strong>缺失可补</strong><span>客户填写，不阻塞报价</span></div>
      <div><strong>交付清楚</strong><span>Excel 与 PDF 同步生成</span></div>
    </div>
  </section>
  <section class="landing-flow" aria-label="报价流程">
    <article><span>01</span><h2>建立项目</h2><p>按客户或工程建立项目，报价、资料和记录都归在项目下。</p></article>
    <article><span>02</span><h2>准备资料</h2><p>上传图纸、模板或参考资料，保留文件版本和校验码。</p></article>
    <article><span>03</span><h2>核对图纸</h2><p>上传客户图纸和模板，检查候选设备、管线与疑点。</p></article>
    <article><span>04</span><h2>核对与补充</h2><p>先算图上已有的部分，缺的室外机或管线可以现场补。</p></article>
    <article><span>05</span><h2>生成报价</h2><p>核对设备、管线、未税、税额和含税合计。</p></article>
    <article><span>06</span><h2>下载归档</h2><p>下载 PDF 和 Excel；同一项目保留多份记录，随时回看。</p></article>
  </section>
</div>"""
    content = f"""{_public_header(user, show_nav=False)}
<main>{body}</main>
{_site_footer()}
{overlay()}
<script>{APP_JS}</script>"""
    return document("HVAC Quote 工程报价工作台", content)


def login_page(
    *,
    error: str | None = None,
    username: str = "",
    next_path: str = "/projects",
    setup_available: bool = False,
) -> str:
    error_html = f'<div class="alert" role="alert">{esc(error)}</div>' if error else ""
    setup_link = (
        '<p class="auth-foot">首次使用？<a href="/setup">创建管理员账号</a></p>'
        if setup_available
        else '<p class="auth-foot">账号由管理员开通；忘记密码请联系管理员重置。</p>'
    )
    body = f"""
<div class="auth-shell">
  <header class="public-header" style="padding-left:0;padding-right:0">
    <a class="identity" href="/">{brand_mark()}
      <span class="identity-text">HVAC Quote<small>工程报价工作台</small></span></a>
    <a class="text-link" href="/">返回首页</a>
  </header>
  <div class="auth-grid">
    <section class="auth-story">
      <p class="eyebrow">HVAC Quote 工程报价工作台</p>
      <h1>复杂留给系统，<br>判断留给你。</h1>
      <p class="lead">图纸有依据，核对有重点，交付有把握。</p>
      <ul class="proof-list">
        <li><b>1</b><span>项目、报价和补充记录归档在一起，不再靠散落的文件找版本。</span></li>
        <li><b>2</b><span>图上没有的信息可以由客户补充，报价不会因此停住。</span></li>
        <li><b>3</b><span>每次生成都保留金额和范围，Excel 与 PDF 随时下载。</span></li>
      </ul>
    </section>
    <section class="auth-card" aria-labelledby="login-title">
      <p class="eyebrow">开始今天的工作</p>
      <h1 id="login-title">欢迎回来</h1>
      <p class="sub">登录，继续你的工程报价。</p>
      {error_html}
      <form method="post" action="/login" data-login>
        <input type="hidden" name="next" value="{esc(next_path)}">
        <label class="field"><span>用户名</span>
          <input name="username" value="{esc(username)}" autocomplete="username" maxlength="32" autofocus placeholder="输入用户名"></label>
        <label class="field"><span>密码</span>
          <input name="password" type="password" autocomplete="current-password" maxlength="128" placeholder="输入密码"></label>
        <button class="btn btn--block" type="submit" data-busy="正在登录…">登录工作台</button>
      </form>
      {setup_link}
    </section>
  </div>
  {_site_footer()}
</div>"""
    return document("登录 · HVAC Quote 工程报价", body, "auth-body")


def setup_page(
    *,
    error: str | None = None,
    values: dict | None = None,
) -> str:
    values = values or {}
    error_html = f'<div class="alert" role="alert">{esc(error)}</div>' if error else ""
    body = f"""
<div class="auth-shell">
  <header class="public-header" style="padding-left:0;padding-right:0">
    <a class="identity" href="/">{brand_mark()}
      <span class="identity-text">HVAC Quote<small>工程报价工作台</small></span></a>
  </header>
  <div class="auth-grid">
    <section class="auth-story">
      <p class="eyebrow">首次使用</p>
      <h1>先创建一个<br>管理员账号。</h1>
      <p class="lead">账号只保存在这台电脑上，密码不会上传，也没有默认口令。</p>
      <div class="setup-hint">创建后，使用该账号登录并建立第一个项目。需要给同事或客户开通账号时，可由管理员在部署机器上添加。</div>
    </section>
    <section class="auth-card" aria-labelledby="setup-title">
      <p class="eyebrow">本机初始化</p>
      <h1 id="setup-title">创建管理员</h1>
      <p class="sub">这一步只需要做一次。</p>
      {error_html}
      <form method="post" action="/setup" data-login>
        <label class="field"><span>显示名称</span>
          <input name="display_name" value="{esc(values.get('display_name', ''))}" maxlength="40" autofocus placeholder="例如：项目管理员"></label>
        <label class="field"><span>用户名</span>
          <input name="username" value="{esc(values.get('username', ''))}" maxlength="32" autocomplete="username" placeholder="2-32 位字母、数字或 ._-"></label>
        <label class="field"><span>密码</span>
          <input name="password" type="password" minlength="8" maxlength="128" autocomplete="new-password" placeholder="至少 8 位"></label>
        <label class="field"><span>确认密码</span>
          <input name="password_confirm" type="password" minlength="8" maxlength="128" autocomplete="new-password" placeholder="再输入一次"></label>
        <button class="btn btn--block" type="submit" data-busy="正在创建…">创建并进入工作台</button>
      </form>
      <p class="auth-foot">已经创建过账号？<a href="/login">返回登录</a></p>
    </section>
  </div>
  {_site_footer()}
</div>"""
    return document("初始化 · HVAC Quote 工程报价", body, "auth-body")


def _contract_status_options(selected: str = "draft") -> str:
    labels = {
        "draft": "草稿",
        "signed": "已签署",
        "effective": "已生效",
        "void": "已作废",
    }
    return "".join(
        f'<option value="{esc(value)}" {"selected" if selected == value else ""}>'
        f"{esc(label)}</option>"
        for value, label in labels.items()
    )


def _contract_file_options(project: Project, selected: str | None = None) -> str:
    options = ['<option value="">不关联原件</option>']
    for item in project.files:
        if item.kind not in {"contract", "reference"}:
            continue
        options.append(
            f'<option value="{esc(item.id)}" '
            f'{"selected" if selected == item.id else ""}>'
            f"{esc(item.original_name)}</option>"
        )
    return "".join(options)


def contracts_page(
    user: dict,
    project: Project,
    contracts: tuple[dict, ...],
    *,
    error: str | None = None,
    values: dict | None = None,
) -> str:
    values = values or {}
    rows = "".join(
        "<tr>"
        f'<td><a href="/projects/{esc(project.id)}/contracts/{esc(item.get("id"))}">'
        f'{esc(item.get("title"))}</a><br>'
        f'<span class="muted">{esc(item.get("contractNo") or "未填合同号")}</span></td>'
        f'<td>{esc(item.get("statusLabel"))}</td>'
        f'<td class="num">{esc(money(item.get("amount")) if item.get("amount") else "—")}</td>'
        f'<td class="num">{esc(money(item.get("settlementAmount")) if item.get("settlementAmount") else "—")}</td>'
        f'<td>{esc(item.get("signedDate") or "—")}</td>'
        "</tr>"
        for item in contracts
    ) or '<tr><td colspan="5">还没有合同记录。</td></tr>'
    error_html = f'<div class="alert" role="alert">{esc(error)}</div>' if error else ""
    body = f"""
<a class="back-link" href="/projects/{esc(project.id)}">← 返回项目</a>
<div class="page-head">
  <div>
    <p class="eyebrow">{esc(project.name)} · 合同</p>
    <h1>合同登记</h1>
    <p class="lead">合同额、结算额和报价额分开保存；报价金额不会自动变成合同额或应收。</p>
  </div>
</div>
{error_html}
<div class="card">
  <h2>已有合同</h2>
  <div class="table-wrap"><table class="table">
    <thead><tr><th>合同</th><th>状态</th><th>合同额</th><th>结算额</th><th>签署日期</th></tr></thead>
    <tbody>{rows}</tbody>
  </table></div>
</div>
<div class="card">
  <h2>新增合同</h2>
  <form method="post" action="/projects/{esc(project.id)}/contracts" data-loading="正在保存合同…">
    <div class="form-grid">
      <label class="field field--wide"><span>合同名称 *</span>
        <input name="title" value="{esc(values.get('title', ''))}" maxlength="100" placeholder="例如：XX项目空调安装合同"></label>
      <label class="field"><span>合同号</span><input name="contract_no" value="{esc(values.get('contract_no', ''))}" maxlength="80"></label>
      <label class="field"><span>状态</span><select name="status">{_contract_status_options(str(values.get('status') or 'draft'))}</select></label>
      <label class="field"><span>签约主体</span><input name="signing_entity" value="{esc(values.get('signing_entity', ''))}" maxlength="100" placeholder="我方签约公司"></label>
      <label class="field"><span>对方</span><input name="counterparty" value="{esc(values.get('counterparty', project.customer))}" maxlength="100"></label>
      <label class="field"><span>合同额</span><input name="amount" value="{esc(values.get('amount', ''))}" inputmode="decimal" placeholder="0.00"></label>
      <label class="field"><span>结算额</span><input name="settlement_amount" value="{esc(values.get('settlement_amount', ''))}" inputmode="decimal" placeholder="可留空"></label>
      <label class="field"><span>签署日期</span><input name="signed_date" type="date" value="{esc(values.get('signed_date', ''))}"></label>
      <label class="field"><span>原件/参考文件</span><select name="file_id">{_contract_file_options(project, str(values.get('file_id') or ""))}</select></label>
      <label class="field field--wide"><span>付款条件</span>
        <textarea name="payment_terms" maxlength="2000" rows="3" placeholder="例如：预付款 30%，进场 40%，验收 25%，质保 5%">{esc(values.get('payment_terms', ''))}</textarea></label>
      <label class="field field--wide"><span>备注</span><input name="note" value="{esc(values.get('note', ''))}" maxlength="1000"></label>
    </div>
    <p class="form-note">合同原件请先在“项目资料”上传，资料类型选“合同/协议”，再在这里关联。</p>
    <div class="form-actions"><button class="btn" type="submit">保存合同</button></div>
  </form>
</div>
"""
    return app_page(
        f"合同 · {project.name}",
        body,
        user,
        active="projects",
        project=project,
        crumbs=_crumb(
            (("我的项目", "/projects"), (project.name, f"/projects/{project.id}"), ("合同", None))
        ),
    )


def contract_detail_page(
    user: dict,
    project: Project,
    contract: dict,
    *,
    error: str | None = None,
) -> str:
    error_html = f'<div class="alert" role="alert">{esc(error)}</div>' if error else ""
    body = f"""
<a class="back-link" href="/projects/{esc(project.id)}/contracts">← 返回合同列表</a>
<div class="page-head">
  <div>
    <p class="eyebrow">{esc(project.name)} · 合同</p>
    <h1>{esc(contract.get("title"))}</h1>
    <p class="lead">{esc(contract.get("statusLabel"))} · {esc(contract.get("contractNo") or "未填合同号")}</p>
  </div>
</div>
{error_html}
<div class="card">
  <form method="post" action="/projects/{esc(project.id)}/contracts/{esc(contract.get('id'))}" data-loading="正在保存合同…">
    <div class="form-grid">
      <label class="field field--wide"><span>合同名称 *</span><input name="title" value="{esc(contract.get('title'))}" maxlength="100"></label>
      <label class="field"><span>合同号</span><input name="contract_no" value="{esc(contract.get('contractNo'))}" maxlength="80"></label>
      <label class="field"><span>状态</span><select name="status">{_contract_status_options(str(contract.get('status')))}</select></label>
      <label class="field"><span>签约主体</span><input name="signing_entity" value="{esc(contract.get('signingEntity'))}" maxlength="100"></label>
      <label class="field"><span>对方</span><input name="counterparty" value="{esc(contract.get('counterparty'))}" maxlength="100"></label>
      <label class="field"><span>合同额</span><input name="amount" value="{esc(contract.get('amount') or '')}" inputmode="decimal"></label>
      <label class="field"><span>结算额</span><input name="settlement_amount" value="{esc(contract.get('settlementAmount') or '')}" inputmode="decimal"></label>
      <label class="field"><span>签署日期</span><input name="signed_date" type="date" value="{esc(contract.get('signedDate') or '')}"></label>
      <label class="field"><span>原件/参考文件</span><select name="file_id">{_contract_file_options(project, contract.get('fileId'))}</select></label>
      <label class="field field--wide"><span>付款条件</span>
        <textarea name="payment_terms" maxlength="2000" rows="4">{esc(contract.get('paymentTerms'))}</textarea></label>
      <label class="field field--wide"><span>备注</span><input name="note" value="{esc(contract.get('note'))}" maxlength="1000"></label>
    </div>
    <div class="form-actions">
      <button class="btn" type="submit">保存修改</button>
      {f'<button class="btn btn--ghost" type="submit" name="action" value="delete" data-confirm="删除这份草稿合同？">删除草稿</button>' if contract.get("status") == "draft" else ''}
    </div>
  </form>
</div>
"""
    return app_page(
        f"{contract.get('title')} · 合同",
        body,
        user,
        active="projects",
        project=project,
        crumbs=_crumb(
            (("我的项目", "/projects"), (project.name, f"/projects/{project.id}"), ("合同", f"/projects/{project.id}/contracts"), (contract.get("title") or "", None))
        ),
    )


def customers_page(
    user: dict,
    customers: tuple[dict, ...],
    projects: tuple[Project, ...],
    *,
    error: str | None = None,
    values: dict | None = None,
) -> str:
    values = values or {}
    counts: dict[str, int] = {}
    for project in projects:
        if project.customer_id:
            counts[project.customer_id] = counts.get(project.customer_id, 0) + 1
    cards = []
    for customer in customers:
        contacts = customer.get("contacts") or ()
        primary = next(
            (contact for contact in contacts if contact.get("isPrimary")),
            contacts[0] if contacts else None,
        )
        contact_text = "、".join(
            part
            for part in (
                primary.get("name") if primary else "",
                primary.get("phone") if primary else "",
            )
            if part
        ) or "还没有联系人"
        cards.append(
            f'<a class="project-card" href="/customers/{esc(customer.get("id"))}">'
            '<div class="card-main"><div class="card-top">'
            f'<span class="badge badge--info">{counts.get(customer.get("id"), 0)} 个项目</span>'
            f'<span class="muted" style="font-size:11px">{esc(date_text(customer.get("updatedAt") or ""))}</span>'
            "</div>"
            f'<h2>{esc(customer.get("name"))}</h2>'
            f'<p class="customer">{esc(customer.get("shortName") or "客户")}</p>'
            f'<p class="note">{esc(contact_text)}</p>'
            '<div class="card-foot"><span>查看客户资料</span><span class="arrow">打开 →</span></div>'
            "</div></a>"
        )
    error_html = f'<div class="alert" role="alert">{esc(error)}</div>' if error else ""
    body = f"""
<div class="page-head">
  <div>
    <p class="eyebrow">经营资料</p>
    <h1>客户</h1>
    <p class="lead">客户是长期对象；同一客户可以有多个工程。项目、合同、回款和客户维护都会归到这里。</p>
  </div>
</div>
{error_html}
<div class="quote-summary" style="align-items:start">
  <section class="card">
    <h2>客户列表</h2>
    <div class="project-grid">{''.join(cards) or '<div class="file-empty">还没有客户。先在右侧建立第一个客户。</div>'}</div>
  </section>
  <section class="card">
    <h2>建立客户</h2>
    <form method="post" action="/customers" data-loading="正在保存客户…">
      <div class="form-grid">
        <label class="field field--wide"><span>客户名称 *</span>
          <input name="name" value="{esc(values.get('name', ''))}" maxlength="80" placeholder="例如：XX建设集团"></label>
        <label class="field"><span>简称</span>
          <input name="short_name" value="{esc(values.get('short_name', ''))}" maxlength="40"></label>
        <label class="field"><span>税号</span>
          <input name="tax_id" value="{esc(values.get('tax_id', ''))}" maxlength="40"></label>
        <label class="field field--wide"><span>地址</span>
          <input name="address" value="{esc(values.get('address', ''))}" maxlength="200"></label>
        <label class="field field--wide"><span>备注</span>
          <textarea name="note" maxlength="500" rows="3">{esc(values.get('note', ''))}</textarea></label>
      </div>
      <div class="form-actions"><button class="btn" type="submit">保存客户</button></div>
    </form>
  </section>
</div>
"""
    return app_page(
        "客户 · HVAC Quote经营工作台",
        body,
        user,
        active="customers",
        crumbs=_crumb((("客户", None),)),
    )


def customer_page(
    user: dict,
    customer: dict,
    projects: tuple[Project, ...],
    *,
    error: str | None = None,
) -> str:
    contacts = customer.get("contacts") or ()
    contact_rows = "".join(
        "<tr>"
        f"<td>{esc(contact.get('name'))}"
        f"{' <span class=\"badge badge--ok\">主要</span>' if contact.get('isPrimary') else ''}</td>"
        f"<td>{esc(contact.get('title'))}</td>"
        f"<td>{esc(contact.get('phone'))}</td>"
        f"<td>{esc(contact.get('wechat'))}</td>"
        f"<td>{esc(contact.get('note'))}</td>"
        "</tr>"
        for contact in contacts
    ) or '<tr><td colspan="5">还没有联系人。</td></tr>'
    project_rows = "".join(
        f'<div class="file-row"><div><div class="file-name">'
        f'<a href="/projects/{esc(project.id)}">{esc(project.name)}</a></div>'
        f'<div class="file-meta">{esc(project.note or "未填写备注")}</div></div>'
        f'<div class="file-size">{len(project.quotes)} 份报价</div></div>'
        for project in projects
    ) or '<div class="file-empty">这个客户还没有项目。</div>'
    error_html = f'<div class="alert" role="alert">{esc(error)}</div>' if error else ""
    body = f"""
<a class="back-link" href="/customers">← 返回客户列表</a>
<div class="page-head">
  <div>
    <p class="eyebrow">客户资料</p>
    <h1>{esc(customer.get("name"))}</h1>
    <p class="lead">{esc(customer.get("shortName") or "未填写简称")}{(" · " + esc(customer.get("taxId"))) if customer.get("taxId") else ""}</p>
  </div>
  <div class="page-actions">
    <a class="btn btn--ghost" href="/projects/new">+ 新建项目</a>
  </div>
</div>
{error_html}
<div class="quote-summary" style="align-items:start">
  <section class="card">
    <h2>客户信息</h2>
    <p class="muted">地址：{esc(customer.get("address") or "未填写")}</p>
    <p class="muted">备注：{esc(customer.get("note") or "未填写")}</p>
    <p class="muted">创建于 {esc(datetime_text(customer.get("createdAt") or ""))}</p>
  </section>
  <section class="card">
    <h2>联系人</h2>
    <div class="table-wrap"><table class="table">
      <thead><tr><th>姓名</th><th>职务</th><th>电话</th><th>微信</th><th>备注</th></tr></thead>
      <tbody>{contact_rows}</tbody>
    </table></div>
    <h3 style="margin-top:18px">新增联系人</h3>
    <form method="post" action="/customers/{esc(customer.get('id'))}/contacts" data-loading="正在保存联系人…">
      <div class="form-grid">
        <label class="field"><span>姓名 *</span><input name="name" maxlength="40" required></label>
        <label class="field"><span>职务</span><input name="title" maxlength="40"></label>
        <label class="field"><span>电话</span><input name="phone" maxlength="40"></label>
        <label class="field"><span>微信</span><input name="wechat" maxlength="60"></label>
        <label class="field field--wide"><span>备注</span><input name="note" maxlength="300"></label>
        <label class="check-line field--wide"><input type="checkbox" name="is_primary">设为主要联系人</label>
      </div>
      <div class="form-actions"><button class="btn btn--small" type="submit">添加联系人</button></div>
    </form>
  </section>
</div>
<div class="section-head"><h2>客户项目</h2><span class="muted">{len(projects)} 个</span></div>
<div class="card">{project_rows}</div>
"""
    return app_page(
        f"{customer.get('name')} · 客户资料",
        body,
        user,
        active="customers",
        crumbs=_crumb((("客户", "/customers"), (customer.get("name") or "", None))),
    )


def projects_page(user: dict, projects: tuple[Project, ...]) -> str:
    quote_count = sum(len(project.quotes) for project in projects)
    updated = projects[0].updated_at if projects else ""
    if projects:
        cards = "".join(
            _project_card(
                project, shared=project.owner != user.get("username")
            )
            for project in projects
        )
        list_html = f"""
<div class="project-toolbar">
  <strong style="font-size:13px">全部项目 <span class="muted">（{len(projects)}）</span></strong>
  <input id="project-filter" class="project-filter" placeholder="筛选项目名称" aria-label="筛选项目名称">
</div>
<div class="project-grid">{cards}</div>"""
    else:
        list_html = """
<div class="card empty">
  <h2>还没有项目</h2>
  <p>每份报价都归属于一个项目。先建立第一个项目，再选择楼层和报价范围。</p>
  <a class="btn" href="/projects/new">建立第一个项目</a>
</div>"""
    body = f"""
<div class="page-head">
  <div>
    <p class="eyebrow">工作台</p>
    <h1>项目总览</h1>
    <p class="lead">找到正在进行的项目，接着上次的工作继续。</p>
  </div>
  <div class="page-actions"><a class="btn" href="/projects/new">+ 新建项目</a></div>
</div>
{getting_started_card()}
<div class="stat-row">
  <div class="stat"><small>项目</small><strong>{len(projects)}</strong></div>
  <div class="stat"><small>报价记录</small><strong>{quote_count}</strong></div>
  <div class="stat"><small>最近更新</small><strong style="font-size:16px;line-height:1.5">{esc(datetime_text(updated) if updated else "—")}</strong></div>
</div>
{list_html}
"""
    return app_page(
        "项目总览 · HVAC Quote 工程报价",
        body,
        user,
        active="projects",
        crumbs=_crumb((("我的项目", None),)),
    )


def _project_card(project: Project, *, shared: bool = False) -> str:
    count = len(project.quotes)
    latest = project.quotes[0] if project.quotes else None
    foot = (
        f"{latest.status_label} · {money(latest.total) if latest.total else '待补充'}"
        if latest
        else "还没有报价"
    )
    shared_badge = (
        '<span class="badge badge--warn" title="由项目所有者共享给你">共享</span>'
        if shared
        else ""
    )
    return f"""
<div class="project-card" data-project-name="{esc(project.name)}">
  <a class="card-main" href="/projects/{esc(project.id)}">
    <div class="card-top"><span class="badge badge--info">{count} 份报价</span>
      {shared_badge}
      <span class="muted" style="font-size:11px">{esc(date_text(project.updated_at))}</span></div>
    <h2>{esc(project.name)}</h2>
    <p class="customer">{esc(project.customer or "未填写客户")}</p>
    <p class="note">{esc(project.note or "按客户或工程归档报价，历史版本自动保留。")}</p>
    <div class="card-foot"><span>{esc(foot)}</span><span class="arrow">打开 →</span></div>
  </a>
</div>"""


def new_project_page(
    user: dict,
    *,
    customers: tuple[dict, ...] = (),
    error: str | None = None,
    values: dict | None = None,
) -> str:
    values = values or {}
    error_html = f'<div class="alert" role="alert">{esc(error)}</div>' if error else ""
    selected_customer = str(values.get("customer_id") or "")
    customer_options = ['<option value="">不关联客户档案（只填名称）</option>']
    for customer in customers:
        customer_options.append(
            f'<option value="{esc(customer.get("id"))}" '
            f'{"selected" if selected_customer == customer.get("id") else ""}>'
            f'{esc(customer.get("name"))}</option>'
        )
    body = f"""
<a class="back-link" href="/projects">← 返回项目总览</a>
<div class="step-layout">
  <div class="step-main">
    <div class="form-card">
      <p class="eyebrow">新项目</p>
      <h1>建立项目</h1>
      <p class="lead">项目名称可以写客户或工程名称；同一个项目下的多次报价会归在一起。</p>
      {error_html}
      <form method="post" action="/projects">
        <div class="form-grid">
          <label class="field field--wide"><span>项目名称 *</span>
            <input name="name" value="{esc(values.get('name', ''))}" maxlength="60" autofocus placeholder="例如：XX学校空调工程"></label>
          <label class="field"><span>客户/甲方</span>
            <input name="customer" value="{esc(values.get('customer', ''))}" maxlength="60" placeholder="例如：XX学校"></label>
          <label class="field"><span>关联客户档案</span>
            <select name="customer_id">{''.join(customer_options)}</select></label>
          <label class="field field--wide"><span>备注</span>
            <textarea name="note" maxlength="500" rows="3" placeholder="项目地址、联系人、范围说明等">{esc(values.get('note', ''))}</textarea></label>
        </div>
        <p class="form-note">创建后可以在“项目资料”里上传图纸、模板和参考资料。选择客户档案后，项目会出现在该客户的资料页；不选也可以只填客户名称。客户档案在左侧“客户”中管理。</p>
        <div class="form-actions">
          <button class="btn" type="submit">创建项目</button>
          <a class="btn btn--ghost" href="/projects">取消</a>
        </div>
      </form>
    </div>
  </div>
  <aside class="step-help">
    <p class="eyebrow">建项目要填什么</p>
    <h2>先把归档单位定下来</h2>
    <ul>
      <li><strong>项目名称：</strong>客户或工程名称，后续报价都归在这里。</li>
      <li><strong>客户/甲方：</strong>方便区分同一客户的不同工程。</li>
      <li><strong>备注：</strong>地址、联系人、范围说明，可留空。</li>
    </ul>
    <p>上传资料后，请在项目资料页完成“检查图纸”和“导入价格”；系统不会仅凭上传自动出价。</p>
    <a class="help-link" href="/help#project-files">查看项目与资料说明 →</a>
  </aside>
</div>
"""
    return app_page(
        "新建项目 · HVAC Quote 工程报价",
        body,
        user,
        active="projects",
        crumbs=_crumb((("我的项目", "/projects"), ("新建项目", None))),
    )


def _project_summary(project: Project) -> str:
    included = [
        quote
        for quote in project.quotes
        if quote.status == "ready" and quote.include_in_total
    ]
    total = Decimal("0")
    for quote in included:
        try:
            total += Decimal(str(quote.total))
        except (InvalidOperation, TypeError, ValueError):
            continue
    warning = ""
    if len(included) > 1:
        warning = '<div class="notice notice--info">多份报价独立生成；汇总不会自动去重，请在计入前核对范围。</div>'
    return f"""
<div class="card" style="margin-bottom:16px">
  <div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap">
    <div><h2 style="margin:0 0 4px">项目汇总</h2>
      <p class="muted" style="margin:0">只汇总已生成且标记“计入汇总”的报价；不是合同金额。</p></div>
    <div style="text-align:right"><small class="muted">含税合计</small>
      <div style="font-size:24px;font-weight:650">{money(total) if included else "—"}</div>
      <small class="muted">{len(included)} 份报价计入</small></div>
  </div>
  {warning}
</div>"""


def project_page(
    user: dict, project: Project, *, members: tuple[dict, ...] = ()
) -> str:
    MEMBER_ROLE_LABELS = {"editor": "可编辑", "viewer": "只读"}
    can_manage = user.get("username") == project.owner
    member_rows = "".join(
        f'<div class="file-row"><div><div class="file-name">{esc(member["username"])}</div>'
        f'<div class="file-meta">由 {esc(member.get("addedBy") or "")} 添加 · {esc(datetime_text(member.get("addedAt") or ""))}</div></div>'
        f'<div class="file-size">{esc(MEMBER_ROLE_LABELS.get(member["role"], member["role"]))}</div>'
        f'<div class="file-actions">'
        + (
            f'<form method="post" action="/projects/{esc(project.id)}/members/{esc(member["username"])}/delete" '
            'data-confirm="确定移除这个成员？移除后对方立即失去本项目的访问。">'
            '<button class="text-button" type="submit">移除</button></form>'
            if can_manage
            else ""
        )
        + "</div></div>"
        for member in members
    )
    manage_form = (
        f"""<form method="post" action="/projects/{esc(project.id)}/members" data-loading="正在添加成员…">
  <div class="form-grid">
    <label class="field"><span>账号（需已存在）</span><input name="username" maxlength="32" placeholder="例如 zhangsan" required></label>
    <label class="field"><span>权限</span>
      <select name="role"><option value="editor">可编辑（上传/修改/生成报价）</option><option value="viewer">只读（仅查看和下载）</option></select></label>
  </div>
  <div class="form-actions"><button class="btn btn--small" type="submit">添加成员</button></div>
</form>"""
        if can_manage
        else ""
    )
    customer_text = esc(project.customer or "未填写客户")
    if project.customer_id:
        customer_text = (
            f'<a href="/customers/{esc(project.customer_id)}">'
            f'{customer_text}</a>'
        )
    if project.quotes:
        quotes_html = "".join(_quote_row(project, quote) for quote in project.quotes)
        quotes = f'<div class="quote-list">{quotes_html}</div>'
    else:
        quotes = """
<div class="card empty">
  <h2>还没有报价</h2>
  <p>上传客户图纸和报价模板，检查候选工程量后生成第一份报价。</p>
  <a class="btn" href="/projects/%s/custom">开始第一份报价</a>
</div>""" % esc(project.id)
    body = f"""
<a class="back-link" href="/projects">← 返回项目总览</a>
<div class="page-head">
  <div>
    <p class="eyebrow">项目</p>
    <h1>{esc(project.name)}</h1>
    <p class="lead">{customer_text}{(" · " + esc(project.note)) if project.note else ""}</p>
  </div>
  <div class="page-actions">
    <a class="btn" href="/projects/{esc(project.id)}/custom">+ 新建报价</a>
    <a class="btn btn--ghost" href="/projects/{esc(project.id)}/copper-prices">铜管参考价</a>
    <a class="btn btn--ghost" href="/projects/{esc(project.id)}/contracts">合同</a>
    <a class="btn btn--ghost" href="/projects/{esc(project.id)}/files">项目资料（{len(project.files)}）</a>
    <a class="btn btn--ghost" href="/projects/{esc(project.id)}/delete">删除项目</a>
  </div>
</div>
<div class="meta-strip">
  <span>创建于 <strong>{esc(date_text(project.created_at))}</strong></span>
  <span>最近更新 <strong>{esc(datetime_text(project.updated_at))}</strong></span>
  <span>报价记录 <strong>{len(project.quotes)}</strong></span>
  <span>项目资料 <strong>{len(project.files)}</strong></span>
</div>
<div class="card">
  <h2>项目成员</h2>
  <p class="muted" style="margin:6px 0 12px">所有者 {esc(project.owner)}；可编辑成员可以上传资料、修改和生成报价，只读成员只能查看与下载。</p>
  <div class="file-list">{member_rows or '<div class="file-empty">还没有其他成员；添加后对方登录即可看到这个项目。</div>'}</div>
  {manage_form}
</div>
<div class="section-head"><h2>报价记录</h2><span class="muted" style="font-size:12px">按时间倒序</span></div>
{_project_summary(project)}
{quotes}
"""
    return app_page(
        f"{project.name} · HVAC Quote 工程报价",
        body,
        user,
        active="projects",
        project=project,
        crumbs=_crumb((("我的项目", "/projects"), (project.name, None))),
    )


def _quote_row(project: Project, quote: QuoteRecord) -> str:
    status_class = "badge--warn" if quote.status == "needs_supplement" else "badge--ok"
    supplement_text = "含客户补充" if quote.supplemented else "基础数据"
    if quote.status == "needs_supplement":
        primary = (
            f'<a href="/projects/{esc(project.id)}/quotes/{esc(quote.run_id)}/supplement">继续补充</a>'
        )
        amount = "待补充"
    else:
        primary = f'<a href="/projects/{esc(project.id)}/quotes/{esc(quote.run_id)}">查看报价</a>'
        amount = money(quote.total) if quote.total else "—"
    downloads = ""
    if quote.status == "ready":
        downloads = (
            f'<a href="/projects/{esc(project.id)}/quotes/{esc(quote.run_id)}/download/quote.pdf">PDF</a>'
            f'<a href="/projects/{esc(project.id)}/quotes/{esc(quote.run_id)}/download/quote.xlsx">Excel</a>'
            f'<form method="post" action="/projects/{esc(project.id)}/quotes/{esc(quote.run_id)}/include" style="display:inline">'
            f'<input type="hidden" name="included" value="{"0" if quote.include_in_total else "1"}">'
            f'<button class="text-button" type="submit">'
            f'{"不计入汇总" if quote.include_in_total else "计入汇总"}</button></form>'
        )
    return f"""
<article class="quote-row">
  <div>
    <span class="badge {status_class}">{esc(quote.status_label)}</span>
    <h3>{esc(quote.title)}</h3>
    <p class="meta">{esc(datetime_text(quote.created_at))} · {esc(supplement_text)}</p>
  </div>
  <div class="quote-total"><small>含税合计</small>{esc(amount)}</div>
  <div class="quote-actions">{primary}{downloads}
    <form method="post" action="/projects/{esc(project.id)}/quotes/{esc(quote.run_id)}/delete"
      data-confirm="确定删除这份报价记录？项目页和汇总将不再显示，导出文件一并删除。"
      style="display:inline">
      <button class="text-button" type="submit">删除</button>
    </form>
  </div>
</article>"""


FILE_KIND_LABELS = {
    "drawing": "设计图纸",
    "template": "报价模板",
    "reference": "参考资料",
}
FILE_KIND_CLASSES = {
    "drawing": "",
    "template": "file-kind--template",
    "reference": "file-kind--reference",
}


def project_files_page(
    user: dict,
    project: Project,
    *,
    error: str | None = None,
    values: dict | None = None,
    notice: str | None = None,
    inspections: tuple[dict, ...] = (),
    price_books: tuple[dict, ...] = (),
    batch_job: dict | None = None,
) -> str:
    values = values or {}
    error_html = f'<div class="alert" role="alert">{esc(error)}</div>' if error else ""
    notice_html = (
        f'<div class="notice notice--info">{esc(notice)}</div>' if notice else ""
    )
    inspection_by_file = {
        str(item.get("fileId")): item for item in inspections
    }
    price_by_file = {
        str(item.get("fileId")): item for item in price_books
    }
    drawings = [item for item in project.files if item.kind == "drawing"]
    templates = [item for item in project.files if item.kind == "template"]
    checked_drawings = sum(
        1 for item in drawings if str(item.id) in inspection_by_file
    )
    priced_templates = sum(
        1 for item in templates if str(item.id) in price_by_file
    )
    has_inspection = bool(inspections)
    has_price = bool(price_books)
    can_create = has_inspection and has_price
    rows = []
    for item in project.files:
        kind = item.kind if item.kind in FILE_KIND_LABELS else "reference"
        extra_action = ""
        if item.kind == "drawing":
            inspection = inspection_by_file.get(str(item.id))
            if inspection is not None:
                extra_action = (
                    '<span class="file-state">✓ 已检查</span>'
                    f'<a href="/projects/{esc(project.id)}/inspections/{esc(inspection["id"])}">查看检查</a>'
                    f'<form class="inline-form" method="post" '
                    f'action="/projects/{esc(project.id)}/files/{esc(item.id)}/inspect">'
                    '<input type="hidden" name="force" value="1">'
                    '<button class="text-button" type="submit" '
                    'data-confirm="强制重新检查会建立新的检查记录，旧草稿和报价仍绑定旧快照。继续？">'
                    '重新检查</button></form>'
                )
            else:
                extra_action = (
                    f'<form class="inline-form" method="post" '
                    f'action="/projects/{esc(project.id)}/files/{esc(item.id)}/preflight" '
                    'data-loading="正在预检图纸规模…">'
                    '<button class="btn btn--small btn--ghost" type="submit">预检范围</button></form>'
                    f'<form class="inline-form" method="post" '
                    f'action="/projects/{esc(project.id)}/files/{esc(item.id)}/inspect" '
                    'data-loading="正在检查图纸…">'
                    '<button class="btn btn--small btn--step" type="submit">检查图纸</button></form>'
                )
        elif item.kind == "template":
            price_book = price_by_file.get(str(item.id))
            if price_book is not None:
                extra_action = (
                    '<span class="file-state">✓ 已导入价格</span>'
                    f'<a href="/projects/{esc(project.id)}/price-books/{esc(price_book["id"])}">查看价格书</a>'
                )
            else:
                extra_action = (
                    f'<form class="inline-form" method="post" '
                    f'action="/projects/{esc(project.id)}/files/{esc(item.id)}/import-template" '
                    'data-loading="正在导入模板价格…">'
                    '<button class="btn btn--small btn--step" type="submit">导入价格</button></form>'
                    f'<a href="/projects/{esc(project.id)}/files/{esc(item.id)}/template-map">识别不对？手工选列</a>'
                )
        rows.append(
            f'<div class="file-row">'
            f'<div><div class="file-name">'
            f'<span class="file-kind {FILE_KIND_CLASSES[kind]}">'
            f"{esc(FILE_KIND_LABELS[kind])}</span>{esc(item.original_name)}</div>"
            f'<div class="file-meta">{esc(datetime_text(item.uploaded_at))} · '
            f"校验码 {esc(item.sha256[:12])}…"
            + (
                f" · {esc(item.content_format)}"
                f"{('（' + esc(item.format_version) + '）') if item.format_version else ''}"
                if getattr(item, "content_format", "")
                else ""
            )
            + "</div></div>"
            f'<div class="file-size">{esc(_file_size(item.size))}</div>'
            f'<div class="file-actions">'
            f"{extra_action}"
            f'<a href="/projects/{esc(project.id)}/files/{esc(item.id)}">下载</a>'
            f'<form method="post" action="/projects/{esc(project.id)}/files/{esc(item.id)}/delete" '
            f'data-confirm="确定删除这份资料？删除后无法恢复。">'
            f'<button class="text-button" type="submit">删除</button></form>'
            f"</div></div>"
        )
    files_html = (
        f'<div class="file-list">{"".join(rows)}</div>'
        if rows
        else '<div class="file-empty">还没有上传资料。先上传客户图纸和 XLSX 报价模板。</div>'
    )
    kind_options = "".join(
        [
            '<option value="auto" '
            + ('selected' if values.get("kind") == "auto" else "")
            + ">自动识别（图纸/模板/参考）</option>"
        ]
        + [
            f'<option value="{key}" {"selected" if values.get("kind") == key else ""}>'
            f"{label}</option>"
            for key, label in FILE_KIND_LABELS.items()
        ]
    )
    drawing_step_state = "done" if checked_drawings and checked_drawings == len(drawings) else ("active" if drawings else "active")
    price_step_state = "done" if priced_templates and priced_templates == len(templates) else ("active" if templates else "active")
    if not drawings:
        drawing_step_state = "active"
    if not templates:
        price_step_state = "active"
    if can_create:
        drawing_step_state = "done"
        price_step_state = "done"
    drawing_action = (
        f'<a href="/projects/{esc(project.id)}/custom">查看检查记录 →</a>'
        if has_inspection
        else (
            '<a href="#upload">先上传图纸 →</a>'
            if not drawings
            else '<span class="muted">在下方图纸行点“检查图纸”</span>'
        )
    )
    price_action = (
        f'<a href="/projects/{esc(project.id)}/custom">查看价格书 →</a>'
        if has_price
        else (
            '<a href="#upload">先上传模板 →</a>'
            if not templates
            else '<span class="muted">在下方模板行点“导入价格”</span>'
        )
    )
    create_action = (
        f'<a class="btn btn--small" href="/projects/{esc(project.id)}/custom">创建报价草稿 →</a>'
        if can_create
        else '<span class="muted">完成检查图纸和导入价格后解锁</span>'
    )
    pending_drawings = sum(
        1 for item in drawings if str(item.id) not in inspection_by_file
    )
    pending_templates = sum(
        1 for item in templates if str(item.id) not in price_by_file
    )
    pending_count = pending_drawings + pending_templates
    batch_running = bool(batch_job and batch_job.get("status") == "running")
    batch_state_labels = {
        "inspected": "✓ 已检查",
        "imported": "✓ 已导入价格",
        "error": "✕ 失败",
    }
    batch_html = ""
    if batch_running:
        done = int(batch_job.get("done") or 0)
        total = int(batch_job.get("total") or 0)
        result_lines = "".join(
            f'<li>{esc(entry.get("fileName") or "")} · '
            f'{batch_state_labels.get(str(entry.get("state")), entry.get("state"))}'
            + (
                f'：{esc(entry.get("message") or "")}'
                if entry.get("state") == "error"
                else ""
            )
            + "</li>"
            for entry in batch_job.get("results") or []
        )
        batch_html = f"""
<div class="card">
  <h2>批量处理进行中</h2>
  <p>已处理 {done} / {total}；当前：{esc(batch_job.get("current") or "—")}。后台逐份执行，本页每 2 秒自动刷新，期间可以离开本页。</p>
  <ul class="excluded">{result_lines}</ul>
</div>
<script>setTimeout(function(){{window.location.reload()}},2000);</script>"""
    elif batch_job and batch_job.get("results"):
        entries = batch_job.get("results") or []
        ok_count = sum(1 for entry in entries if entry.get("state") != "error")
        failed_count = sum(1 for entry in entries if entry.get("state") == "error")
        summary = f"成功 {ok_count}" + (f"，失败 {failed_count}" if failed_count else "") + "。"
        result_lines = "".join(
            f'<li>{esc(entry.get("fileName") or "")} · '
            f'{batch_state_labels.get(str(entry.get("state")), entry.get("state"))}'
            + (
                f'：{esc(entry.get("message") or "")}'
                if entry.get("state") == "error"
                else ""
            )
            + "</li>"
            for entry in entries
        )
        batch_html = f"""
<div class="card">
  <h2>批量处理完成</h2>
  <p>{esc(summary)}失败的文件可修正后重新上传，再单独处理或重新批量处理。</p>
  <ul class="excluded">{result_lines}</ul>
</div>"""
    batch_button = (
        f'<form method="post" action="/projects/{esc(project.id)}/files/batch-process" '
        'data-loading="正在开始批量处理…" style="display:inline">'
        f'<button class="btn btn--ghost" type="submit">一键检查 / 导入（{pending_count} 份待处理）</button></form>'
        if pending_count and not batch_running
        else ""
    )
    body = f"""
{_history_back("返回上一页", f"/projects/{project.id}")}
<div class="page-head">
  <div>
    <p class="eyebrow">{esc(project.name)}</p>
    <h1>项目资料</h1>
    <p class="lead">上传图纸和客户模板，然后按下面四步完成报价；上传本身不会自动出价。</p>
  </div>
  <div class="page-actions">
    {batch_button}
    <a class="btn" href="/projects/{esc(project.id)}/custom">进入报价工作台</a>
  </div>
</div>
{batch_html}
<section class="workflow" aria-label="报价流程">
  <div class="workflow-head">
    <div>
      <h2>报价流程</h2>
      <p>第 2 步和第 3 步都完成后，第 4 步才会解锁。每一步的结果都会保留在本项目里。</p>
    </div>
  </div>
  <ol class="workflow-steps">
    <li class="workflow-step {'done' if project.files else 'active'}">
      <span class="num">1</span><strong>上传图纸和模板</strong>
      <small>{len(drawings)} 份图纸 · {len(templates)} 份模板 · 共 {len(project.files)} 份资料</small>
      <a href="#upload">去上传 / 查看资料 →</a>
    </li>
    <li class="workflow-step {drawing_step_state}">
      <span class="num">2</span><strong>检查图纸</strong>
      <small>{checked_drawings} / {len(drawings)} 份图纸已检查，生成候选设备和管线</small>
      {drawing_action}
    </li>
    <li class="workflow-step {price_step_state}">
      <span class="num">3</span><strong>导入模板价格</strong>
      <small>{priced_templates} / {len(templates)} 份模板已导入，按已确认的列映射读取</small>
      {price_action}
    </li>
    <li class="workflow-step {'active' if can_create else ''}">
      <span class="num">4</span><strong>创建草稿并生成</strong>
      <small>人工确认设备/管线后，生成 Excel、PDF 和页面预览</small>
      {create_action}
    </li>
  </ol>
</section>
<div class="step-layout">
  <div class="step-main">
    <div class="card" id="upload">
      <h2>第 1 步：上传资料</h2>
      {notice_html}
      {error_html}
      <form method="post" action="/projects/{esc(project.id)}/files" enctype="multipart/form-data" data-loading="正在上传并校验文件…">
        <div class="form-grid">
          <label class="field"><span>资料类型</span>
            <select name="kind">{kind_options}</select></label>
          <label class="field"><span>选择文件 *（可多选）</span>
            <input class="file-input" type="file" name="upload" required multiple
              accept=".dwg,.dxf,.xlsx,.xlsm,.pdf"></label>
        </div>
        <p class="form-note">可一次选择多份文件，单个文件不超过 50 MB。图纸 DWG/DXF；模板 XLSX/XLSM；参考资料另支持 PDF。选“自动识别”会按扩展名分别归为图纸/模板/参考；上传后仍需在文件行逐份点“检查图纸”或“导入价格”。</p>
        <div class="form-actions"><button class="btn" type="submit">上传资料</button></div>
      </form>
    </div>
    <div class="card">
      <h2>已上传资料 <span class="muted" style="font-size:12px">（{len(project.files)}）</span></h2>
      <p class="muted">按上传时间倒序。已经检查/导入的文件会显示绿色状态；删除资料不会影响已经生成的报价。</p>
      {files_html}
    </div>
  </div>
  <aside class="step-help">
    <p class="eyebrow">为什么还要点两步</p>
    <h2>上传只是归档</h2>
    <ul>
      <li><strong>检查图纸：</strong>读取图块、图层和几何，生成候选设备与管线，并列出疑点。</li>
      <li><strong>导入价格：</strong>按识别并确认的列映射读取单价，形成可选择的模板行和单价。</li>
      <li><strong>创建草稿：</strong>把候选和价格行放在一起，由你逐组确认数量和长度。</li>
      <li><strong>生成报价：</strong>只有确认后的组才进入 Excel、PDF 和页面金额。</li>
    </ul>
    <a class="help-link" href="/help#project-files">查看资料说明 →</a>
  </aside>
</div>
"""
    return app_page(
        f"项目资料 · {project.name}",
        body,
        user,
        active="projects",
        project=project,
        crumbs=_crumb(
            (
                ("我的项目", "/projects"),
                (project.name, f"/projects/{project.id}"),
                ("项目资料", None),
            )
        ),
    )


def _column_letter(number: int) -> str:
    if number <= 26:
        return chr(64 + number)
    return chr(64 + (number - 1) // 26) + chr(65 + (number - 1) % 26)


def _column_select(name: str, selected: str, max_column: int, *, optional: bool = False) -> str:
    options = ['<option value="">— 不指定 —</option>'] if optional else [
        '<option value="">— 选择列 —</option>'
    ]
    for number in range(1, min(max_column, 30) + 1):
        letter = _column_letter(number)
        options.append(
            f'<option value="{letter}" '
            f'{"selected" if str(selected).upper() == letter else ""}>{letter}</option>'
        )
    return f'<select name="{esc(name)}">{"".join(options)}</select>'


def template_mapping_page(
    user: dict,
    project: Project,
    item,
    tables: list[dict],
    *,
    profiles: list[dict] | None = None,
) -> str:
    profiles = profiles or []
    cards = []
    column_fields = (
        ("name_col", "名称列 *", "name", False),
        ("spec_col", "规格/型号列 *", "spec", False),
        ("unit_col", "单位列 *", "unit", False),
        ("quantity_col", "工程量列（可选）", "quantity", True),
        ("material_col", "材料价列 *", "material", False),
        ("install_col", "安装价列（可选）", "installation", True),
    )
    for table in tables:
        name = str(table.get("name") or "")
        max_column = int(table.get("maxColumn") or 0)
        columns = table.get("columns") or {}
        header_row = table.get("headerRow") or 1
        preview_rows = []
        for preview in table.get("preview") or []:
            cells = []
            for index, value in enumerate(preview.get("values") or [], 1):
                if not value:
                    continue
                cells.append(
                    f'<td><span class="muted">{_column_letter(index)}</span> {esc(value)}</td>'
                )
            preview_rows.append(
                f"<tr><th>第 {esc(preview.get('row'))} 行</th>{''.join(cells)}</tr>"
            )
        detect_text = (
            f"自动识别：第 {header_row} 行表头；"
            + "、".join(
                f"{label} {columns.get(key)}"
                for _, label, key, _ in column_fields
                if columns.get(key)
            )
            if table.get("headerRow")
            else "未自动识别表头，请手工选择列。"
        )
        fields = "".join(
            f'<label class="field"><span>{esc(label)}</span>'
            f'{_column_select(field, columns.get(key) or "", max_column, optional=optional)}</label>'
            for field, label, key, optional in column_fields
        )
        cards.append(
            f"""
<section class="card">
  <h2>{esc(name)} <span class="muted" style="font-size:12px">（{esc(table.get('maxRow'))} 行 × {esc(max_column)} 列）</span></h2>
  <p class="muted">{esc(detect_text)}</p>
  <div class="table-wrap"><table class="table"><tbody>{''.join(preview_rows) or '<tr><td>没有可预览内容</td></tr>'}</tbody></table></div>
  <form method="post" action="/projects/{esc(project.id)}/files/{esc(item.id)}/import-template" data-loading="正在按所选列导入价格…" style="margin-top:14px">
    <input type="hidden" name="sheet" value="{esc(name)}">
    <div class="form-grid">
      <label class="field"><span>表头所在行 *</span>
        <input name="header_row" type="number" min="1" value="{esc(header_row)}" required></label>
      {fields}
    </div>
    <div class="form-actions"><button class="btn" type="submit">按这些列导入价格</button></div>
  </form>
</section>"""
        )
    body = f"""
{_history_back("返回上一页", f"/projects/{project.id}/files")}
<div class="page-head">
  <div>
    <p class="eyebrow">{esc(project.name)} · 模板导入</p>
    <h1>手工选择工作表与列</h1>
    <p class="lead">自动识别不对时，用这里指定 Sheet、表头行和列；不需要重排客户原来的 Excel。</p>
  </div>
  <div class="page-actions">
    <a class="btn btn--small btn--ghost" href="/projects/{esc(project.id)}/template-mappings">已保存的客户映射（{len(profiles)}）</a>
  </div>
</div>
<div class="notice notice--info">名称、规格、单位、材料价四列必填；工程量、安装价可选。安装价为空时按 0 处理，并会写入导入提示。手工导入后会按客户保存映射，下次同类表自动套用。</div>
{''.join(cards) or '<div class="empty">这个工作簿没有可预览的工作表。</div>'}
"""
    return app_page(
        f"模板导入 · {item.original_name}",
        body,
        user,
        active="projects",
        project=project,
        crumbs=_crumb(
            (("我的项目", "/projects"), (project.name, f"/projects/{project.id}"), ("模板导入", None))
        ),
    )


def template_mapping_profiles_page(
    user: dict,
    project: Project,
    profiles: list[dict],
    *,
    notice: str = "",
) -> str:
    labels = {
        "name": "名称",
        "spec": "规格",
        "unit": "单位",
        "quantity": "工程量",
        "material": "材料价",
        "installation": "安装价",
    }
    rows = []
    for profile in profiles:
        columns = profile.get("columns") or {}
        column_text = "、".join(
            f"{label} {columns.get(key)}"
            for key, label in labels.items()
            if columns.get(key)
        )
        rows.append(
            f"""<tr>
  <td>{esc(profile.get('fileName') or '—')}</td>
  <td>{esc(profile.get('sheetName') or '—')}<br><span class="muted">第 {esc(profile.get('headerRow'))} 行表头</span></td>
  <td>{esc(column_text or '—')}</td>
  <td>{esc(datetime_text(profile.get('updatedAt') or ''))}</td>
  <td><form method="post" action="/projects/{esc(project.id)}/template-mappings/{esc(profile.get('id'))}/delete" data-confirm="确定删除这条客户列映射？下次导入会回退自动识别或需要重新手工选列。" style="display:inline"><button class="text-button" type="submit">删除</button></form></td>
</tr>"""
        )
    notice_html = (
        f'<div class="notice notice--success">{esc(notice)}</div>'
        if notice
        else ""
    )
    body = f"""
{_history_back("返回上一页", f"/projects/{project.id}/files")}
<div class="page-head">
  <div>
    <p class="eyebrow">{esc(project.name)}</p>
    <h1>客户模板映射</h1>
    <p class="lead">客户：{esc(project.customer or "账号级")}。手工选列成功后自动保存；下次导入表头相同的报价表会直接套用。</p>
  </div>
  <div class="page-actions">
    <a class="btn btn--small btn--ghost" href="/projects/{esc(project.id)}/files">返回项目资料</a>
  </div>
</div>
{notice_html}
<div class="card">
  <h2>已保存的映射（{len(profiles)}）</h2>
  <p class="muted">删除后不会影响已经导入的价格书或报价；只影响下一次导入时的自动套用。</p>
  <div class="table-wrap"><table class="table">
    <thead><tr><th>来源文件</th><th>Sheet / 表头</th><th>列映射</th><th>更新时间</th><th>操作</th></tr></thead>
    <tbody>{''.join(rows) or '<tr><td colspan="5">还没有保存的客户列映射。到项目资料点“识别不对？手工选列”，导入一次即可保存。</td></tr>'}</tbody>
  </table></div>
</div>
"""
    return app_page(
        f"客户模板映射 · {project.name}",
        body,
        user,
        active="projects",
        project=project,
        crumbs=_crumb(
            (("我的项目", "/projects"), (project.name, f"/projects/{project.id}"), ("客户模板映射", None))
        ),
    )


def _price_options(price_book: dict, selected, category: str) -> str:
    rows = [
        row
        for row in (price_book.get("book") or {}).get("rows", [])
        if row.get("priceState") == "priced"
    ]
    preferred = [row for row in rows if row.get("category") == category]
    choices = preferred or rows
    options = ['<option value="">— 选择模板行 —</option>']
    for row in choices:
        value = str(row.get("rowNumber"))
        system = str(row.get("system") or "")
        prefix = {"water": "水系统 · ", "refrigerant": "冷媒 · "}.get(system, "")
        label = (
            f"{prefix}{row.get('name') or ''} {row.get('spec') or ''}"
            f"（模板第{row.get('rowNumber')}行）"
        ).strip()
        options.append(
            f'<option value="{esc(value)}" '
            f'{"selected" if str(selected) == value else ""}>{esc(label)}</option>'
        )
    return "".join(options)


def _display_view(value: str | None) -> str:
    text = str(value or "").strip()
    if text.lower() in {"", "model", "none"}:
        return "模型空间（未标注楼层）"
    return text


def _display_layer(value: str | None) -> str:
    text = str(value or "").strip()
    if text in {"", "0"}:
        return "通用图层"
    return text


def _issue_text(code: str) -> str:
    return {
        "PIPE_LAYER_UNCERTAIN": "图层不是明确的管线图层，已按附近管径标注列为候选",
        "POLYLINE_SEGMENTS_ONLY": "按折线段长度计算",
        "ARC_APPROXIMATED": "圆弧段按弧长近似",
        "PIPE_SPEC_UNRESOLVED": "没有读到管径标注",
        "PIPE_SPEC_AMBIGUOUS": "附近有多个管径标注",
        "SPLINE_LENGTH_UNVERIFIED": "样条曲线长度未验证",
        "ELLIPSE_LENGTH_UNVERIFIED": "椭圆长度未验证",
        "GEOMETRY_INVALID": "图形数据无法量取",
        "GEOMETRY_UNSUPPORTED": "该图形类型暂不量取",
        "PIPE_IDENTITY_UNRESOLVED": "管线身份或介质证据不足，需按图核对",
        "PIPE_MEDIUM_CONFLICT": "附近有互相冲突的介质/管径标注，需按图核对",
        "PIPE_STYLE_REVIEW": "通用图层的虚线走向像管线，但系统归属未确认",
        "PIPE_LAYER_INHERITED": "图元继承自图块图层，需确认是否为块内真实管线",
        "PIPE_SEGMENT_BOUNDARY_INFERRED": "变径分界为计量提案，需按图确认分歧口径",
        "PIPE_SEGMENT_BOUNDARY_AMBIGUOUS": "变径分歧位置尚不唯一，需按局部原图确认",
        "PIPE_ANNOTATION_UNASSIGNED": "附近标注归属不唯一",
        "DUPLICATE_SCOPE_UNCONFIRMED": "疑似重复图面，未确认前不自动计入",
        "PIPE_SOURCE_SCOPE_UNCONFIRMED": "专业图层来源/图面范围未确认，需按图核对",
        "PIPE_CROSS_MEDIUM_CONTACT": "端部接触的管网介质不一致，未自动传播身份",
        "EQUIPMENT_INTERNAL_ROUTE_REVIEW": "设备图块内的显式管线，需按图核对",
        "EQUIPMENT_SYMBOL_GEOMETRY": "设备符号内部图形仅用于显示，不计入管线",
        "INHERITED_LAYER_CONTEXT_GEOMETRY": "图块符号内部图形仅用于显示，不计入管线",
        "ROUTE_SYMBOL_CIRCLE": "圆/弧符号不按敷设长度累加",
        "GEOMETRY_BOUNDS_UNAVAILABLE": "外框无法可靠计算，需人工核对",
        "ROUTE_ADOPTION_UNCONFIRMED": "管线身份、规格或范围尚未核对",
    }.get(code, "需要人工核对")


def _draft_views_text(data: dict) -> str:
    parts = []
    for view in data.get("views", []):
        label = view.get("floorLabel") or view.get("view") or "未识别"
        if str(label).lower() == "model":
            label = "模型空间（未标注楼层）"
        parts.append(
            f"{label}（{view.get('equipmentGroups', 0)} 设备组 / "
            f"{view.get('routeGroups', 0)} 管线组）"
        )
    return "；".join(parts) or "未识别"


def _confidence_label(value: str) -> str:
    return {
        "exact": "精确匹配",
        "fuzzy": "近似匹配",
        "category_only": "按类别匹配",
        "rule": "已保存规则",
        "spec": "按规格匹配",
        "inferred": "按同系统DN推断",
        "set": "成套合并",
        "set_candidate": "套价候选",
        "set_component": "套内组成",
        "linked": "等长联动",
        "manual": "人工补录",
        "none": "未匹配",
    }.get(value, value or "未匹配")


def _issue_badge(
    severity: str, acknowledged: bool = False, *, label: str | None = None
) -> str:
    if acknowledged:
        return '<span class="badge badge--ok">已确认</span>'
    if severity == "blocks_pricing":
        return f'<span class="badge badge--warn">{esc(label or "阻塞")}</span>'
    return '<span class="badge badge--info">待核对</span>'


def inspection_page(
    user: dict,
    project: Project,
    inspection: dict,
    price_books: tuple[dict, ...],
    *,
    notice: str = "",
    vision_enabled: bool = False,
    vision_config: dict | None = None,
    vision_candidates: list[dict] | None = None,
    vision_drafts: list[dict] | None = None,
) -> str:
    report = inspection.get("report") or {}
    success_notice = (
        "这张图纸已经检查并保存，结果不会丢失；再次点击“检查图纸”会直接回到这里，不会重复生成记录。"
        if notice == "already-inspected"
        else "检查结果已保存。下面这些是候选和疑点，不是已经采用的工程量。"
    )
    counts = report.get("counts") or {}
    issues = report.get("issues") or []
    equipment_rows = "".join(
        "<tr>"
        f"<td>{esc(group.get('layer', ''))}</td>"
        f"<td>{esc(group.get('blockName', ''))}</td>"
        f"<td>{esc(group.get('modelHint', ''))}</td>"
        f'<td class="num">{esc(group.get("quantity", 0))}</td>'
        f"<td>{esc(' / '.join(group.get('textHints', [])[:3]))}</td>"
        "</tr>"
        for group in report.get("equipmentGroups", [])
    )
    route_rows = "".join(
        "<tr>"
        f"<td>{esc(group.get('layer', ''))}</td>"
        f"<td>{esc(group.get('entityType', ''))}</td>"
        f"<td>{esc(group.get('category', ''))}</td>"
        f'<td class="num">{esc(group.get("count", 0))}</td>'
        f'<td class="num">{esc(round(float(group.get("lengthRaw") or 0), 3))}</td>'
        f'<td class="num">{esc(round(float(group.get("lengthM") or 0), 3)) if group.get("lengthM") is not None else "—"}</td>'
        f"<td>{esc(' / '.join(group.get('issues', [])))}</td>"
        "</tr>"
        for group in report.get("routeGroups", [])
    )
    issues_html = "".join(
        f"<li>{_issue_badge(str(item.get('severity', 'review')), label='需核对')} "
        f"{esc(item.get('message', ''))}</li>"
        for item in issues
    ) or "<li>没有发现疑点。</li>"
    if price_books:
        options = "".join(
            f'<option value="{esc(book["id"])}">{esc(book["name"])}'
            f"（{esc((book.get('book') or {}).get('counts', {}).get('rows', 0))} 行）</option>"
            for book in price_books
        )
        floor_summary = report.get("floorSummary") or []
        scope_rows = floor_summary or report.get("viewSummary", [])
        multiple_floors = len(floor_summary) > 1
        view_choices = "".join(
            f'<option value="{esc((str(view.get("view")) + "::" + str(view.get("floorLabel"))) if multiple_floors else str(view.get("view")))}">'
            f'{esc(view.get("floorLabel") or view.get("view"))}'
            f"（设备 {esc(view.get('equipmentGroups', 0))} 组 / "
            f"管线 {esc(view.get('routeGroups', 0))} 组）</option>"
            for view in scope_rows
        )
        scope_note = (
            "同一模型空间检测到多个楼层区域；可以选择整份图纸或单层。"
            if multiple_floors
            else "选择具体视图时，只把该视图/楼层的候选带入草稿；选“全部”则跨视图汇总。"
        )
        create_form = f"""
<form method="post" action="/projects/{esc(project.id)}/custom/drafts" data-loading="正在创建报价草稿…">
  <input type="hidden" name="inspection_id" value="{esc(inspection['id'])}">
  <div class="form-grid">
    <label class="field"><span>价格书 *</span><select name="price_book_id">{options}</select></label>
    <label class="field"><span>本次范围</span>
      <select name="scope">
        <option value="">全部视图/楼层</option>
        {view_choices}
      </select></label>
    <label class="field"><span>代表层数 ×N</span>
      <input name="multiplier_{esc(inspection['id'])}" type="number" min="1" max="20" step="1" value="1"></label>
    <label class="field field--wide"><span>草稿名称</span>
      <input name="title" value="新图纸报价 · {esc(inspection.get('fileName', ''))}"></label>
  </div>
  <p class="muted">{scope_note} 代表层数用于“一张图代表多个相同楼层”；填 2 会把设备和管线按 2 倍计入。</p>
  <div class="form-actions"><button class="btn" type="submit">创建报价草稿</button></div>
</form>"""
    else:
        create_form = '<div class="notice">还没有模板价格书，请先到“项目资料”导入模板价格。</div>'
    config = vision_config or {}
    vision_available = bool(config.get("available"))
    vision_candidates = vision_candidates or []
    vision_drafts = vision_drafts or []
    draft_options = "".join(
        f'<option value="{esc(item["id"])}">'
        f'{esc((item.get("data") or {}).get("title") or "新图纸报价")} · 版本 {esc(item.get("revision") or 1)}'
        "</option>"
        for item in vision_drafts
    )
    candidate_html = "".join(
        '<label class="check-line" style="justify-content:space-between;gap:12px;padding:6px 0;border-bottom:1px solid #eef0f2">'
        f'<span><input type="checkbox" name="candidate_ids" value="{esc(item["candidateId"])}"> '
        f'<strong>{esc(item.get("label") or item.get("sourceKey"))}</strong> '
        f'<span class="muted">{"设备" if item.get("kind") == "equipment" else "管线"} · '
        f'{esc(item.get("layer") or "")} · '
        f'{"数量 " + str(item.get("quantity")) if item.get("kind") == "equipment" else (str(item.get("spec") or "") + " · " + str(item.get("lengthM") or "长度待核") + " 米")}</span>'
        f'{" · 待核：" + esc("、".join(item.get("reviewReasons") or [])) if item.get("reviewReasons") else ""}</span></label>'
        for item in vision_candidates
    )
    if not vision_enabled:
        vision_html = (
            '<div class="card" id="vision"><h2>视觉辅助（试验）</h2>'
            '<p class="muted">当前已关闭。开启后仍需选中具体疑难局部并预览，系统不会自动发送整图，'
            "也不会自动改量或定价。</p>"
            '<a class="btn btn--small" href="/account">到账号设置开启</a></div>'
        )
    elif not vision_drafts:
        vision_html = (
            '<div class="card" id="vision"><h2>视觉辅助（试验）</h2>'
            '<p class="muted">已开启。请先创建报价草稿，再回到检查页或草稿页选择疑难局部。</p></div>'
        )
    else:
        vision_html = f"""
<div class="card" id="vision">
  <h2>视觉辅助（试验）</h2>
  <p class="muted">模型状态：{'已配置' if vision_available else '未配置/不可用'}。
  仅对下面选中的局部生成建议；数量、长度仍由 CAD 重算，价格仍来自客户模板。
  每次新任务都需要你主动提交。</p>
  <form method="post" action="/projects/{esc(project.id)}/inspections/{esc(inspection["id"])}/vision-preview">
    <div class="form-grid">
      <label class="field"><span>目标草稿 *</span>
        <select name="draft_id">{draft_options}</select></label>
    </div>
    <div class="check-list" style="max-height:340px;overflow:auto">{candidate_html or '<p class="muted">这份检查没有可渲染的设备/管线候选。</p>'}</div>
    <div class="form-actions"><button class="btn btn--small" type="submit" {'disabled' if not candidate_html else ''}>预览选中局部</button></div>
  </form>
</div>"""
    floor_summary = report.get("floorSummary") or []
    floor_html = ""
    if len(floor_summary) > 1:
        confidence_labels = {
            "region": "区域标注",
            "nearest": "最近标注",
            "unresolved": "未识别",
            "single-floor": "单层",
        }
        floor_rows = "".join(
            "<tr>"
            f"<td>{esc(item.get('floorLabel'))}</td>"
            f"<td>{esc(item.get('equipmentGroups', 0))}</td>"
            f"<td>{esc(item.get('routeGroups', 0))}</td>"
            f"<td>{esc(item.get('entityCount', 0))}</td>"
            f"<td>{esc('—' if not item.get('bbox') else ', '.join(str(round(float(value), 1)) for value in item.get('bbox')))}</td>"
            f"<td>{esc(confidence_labels.get(str(item.get('confidence')), '需核对'))}</td>"
            "</tr>"
            for item in floor_summary
        )
        floor_html = f"""
<div class="card">
  <h2>同模型空间楼层区域</h2>
  <p class="muted">按图面空白和楼层标注做逻辑裁剪，只影响候选归属，不修改原图；边界不清时保留“未识别”，不强行归层。</p>
  <div class="table-wrap"><table class="table">
    <thead><tr><th>楼层</th><th>设备组</th><th>管线组</th><th>区域图元</th><th>区域范围（图面单位）</th><th>判定</th></tr></thead>
    <tbody>{floor_rows}</tbody>
  </table></div>
</div>"""
    body = f"""
{_history_back("返回上一页", f"/projects/{project.id}/files")}
<div class="page-head">
  <div>
    <p class="eyebrow">{esc(project.name)} · 图纸检查</p>
    <h1>{esc(inspection.get('fileName', '图纸检查'))}</h1>
    <p class="lead">图块、图层和几何已读取；下面是候选分组和疑点，不是已采用的工程量。</p>
  </div>
</div>
<div class="notice notice--success">✓ {esc(success_notice)}</div>
<div class="kpis">
  <div class="kpi"><small>图元</small><strong>{esc(counts.get('entities', 0))}</strong></div>
  <div class="kpi"><small>候选设备组</small><strong>{esc(counts.get('equipmentGroups', 0))}</strong></div>
  <div class="kpi"><small>候选管线组</small><strong>{esc(counts.get('routeGroups', 0))}</strong></div>
</div>
<div class="card">
  <h2>图纸依据</h2>
  <p class="muted">单位：{esc((report.get('units') or {}).get('measurementUnit') or '未识别')}；
  比例：{esc((report.get('units') or {}).get('metresPerDrawingUnit') or '未确认')} 米/图面单位；
  视图：{esc(' / '.join(str(v.get('view')) for v in report.get('views', [])) or 'model')}；
  检查版本：{esc(report.get('engineVersion') or '旧版本')}；
  检查时间：{esc(datetime_text(inspection.get('createdAt') or ''))}。</p>
  <p class="muted">本次范围：{esc(json.dumps(report.get('inspectionScope') or {}, ensure_ascii=False))}。
  旧版本检查仍可查看；强制重新检查会建立新记录，历史草稿和报价继续绑定旧快照。</p>
  <h3>疑点</h3>
  <p class="muted">这里是图纸读取阶段的疑点；进入报价草稿后，系统会再区分“必须补资料”和“可确认后继续”，以草稿页“还要补什么”为准。</p>
  <ul class="excluded">{issues_html}</ul>
</div>
{floor_html}
<div class="group-card">
  <h2>候选设备组</h2>
  <div class="table-wrap"><table class="table">
    <thead><tr><th>图层</th><th>图块</th><th>型号提示</th><th>数量</th><th>附近文字</th></tr></thead>
    <tbody>{equipment_rows or '<tr><td colspan="5">没有候选设备</td></tr>'}</tbody>
  </table></div>
</div>
<div class="group-card">
  <h2>候选管线组</h2>
  <div class="table-wrap"><table class="table">
    <thead><tr><th>图层</th><th>类型</th><th>类别</th><th>数量</th><th>图面长度</th><th>米制长度</th><th>备注</th></tr></thead>
    <tbody>{route_rows or '<tr><td colspan="7">没有候选管线</td></tr>'}</tbody>
  </table></div>
</div>
{vision_html}
<div class="card">
  <h2>下一步：人工确认并生成草稿</h2>
  <p class="muted">草稿会按型号/图层自动匹配模板行；匹配不到或比例不确定的组会进入异常清单，由你确认后再生成报价。</p>
  {create_form}
</div>
"""
    return app_page(
        f"图纸检查 · {inspection.get('fileName', '')}",
        body,
        user,
        active="projects",
        project=project,
        crumbs=_crumb(
            (("我的项目", "/projects"), (project.name, f"/projects/{project.id}"), ("图纸检查", None))
        ),
    )


def price_book_page(
    user: dict,
    project: Project,
    price_book: dict,
    *,
    notice: str | None = None,
    error: str | None = None,
    is_account_default: bool = False,
) -> str:
    book = price_book.get("book") or {}
    notice_html = (
        f'<div class="notice notice--info">{esc(notice)}</div>' if notice else ""
    )
    error_html = f'<div class="alert" role="alert">{esc(error)}</div>' if error else ""
    rows = []
    for row in book.get("rows", []):
        state = (
            '<span class="badge badge--ok">可用</span>'
            if row.get("priceState") == "priced"
            else '<span class="badge badge--warn">待重算</span>'
        )
        row_number = row.get("rowNumber")
        base = f"/projects/{esc(project.id)}/price-books/{esc(price_book.get('id'))}/rows/{esc(row_number)}"
        delete_button = ""
        if row.get("projectPrice"):
            delete_button = (
                f'<form method="post" action="{base}/delete" '
                'data-confirm="确定删除这行项目价？删除后草稿里选过它的地方需要重新选行。" '
                'style="display:inline">'
                '<button class="text-button" type="submit">删行</button></form>'
            )
        rows.append(
            "<tr>"
            f"<td>{esc(row_number)}</td>"
            f"<td>{esc(row.get('index'))}</td>"
            f"<td>{esc(row.get('name'))}</td>"
            f"<td>{esc(row.get('spec'))}</td>"
            f"<td>{esc(row.get('unit'))}</td>"
            f'<td class="num">{esc(row.get("material") or "—")}</td>'
            f'<td class="num">{esc(row.get("installation") or "—")}</td>'
            f"<td>{esc(row.get('category'))}</td>"
            f"<td>{state}</td>"
            "<td>"
            f'<form method="post" action="{base}" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">'
            f'<input name="material" type="number" min="0" step="0.01" value="{esc(row.get("material") or "")}" style="width:90px;padding:7px 9px;border:1px solid #cbd2dc;border-radius:8px" title="材料价">'
            f'<input name="installation" type="number" min="0" step="0.01" value="{esc(row.get("installation") or "")}" style="width:90px;padding:7px 9px;border:1px solid #cbd2dc;border-radius:8px" title="安装价">'
            f'<input name="unit" value="{esc(row.get("unit") or "")}" style="width:56px;padding:7px 9px;border:1px solid #cbd2dc;border-radius:8px" title="单位">'
            '<button class="text-button" type="submit">保存</button>'
            f"</form>{delete_button}"
            "</td>"
            "</tr>"
        )
    warnings = "".join(f"<li>{esc(item)}</li>" for item in book.get("warnings", []))
    columns = book.get("columns") or {}
    column_labels = {
        "name": "名称",
        "spec": "规格",
        "unit": "单位",
        "quantity": "工程量",
        "material": "材料价",
        "installation": "安装价",
    }
    column_text = "、".join(
        f"{label} {columns[key]}"
        for key, label in column_labels.items()
        if key in columns
    ) or "B 名称、C 型号、D 单位、G 材料价、H 安装价"
    source_text = (
        f"工作表“{book.get('sheetName') or '第一个工作表'}”"
        f"{('第 ' + str(book.get('headerRow')) + ' 行表头；') if book.get('headerRow') else '；'}"
        f"列映射：{column_text}。只读取事实，不修改母模板。"
    )
    detected_by = str(book.get("detectedBy") or "")
    if detected_by == "auto":
        source_notice = (
            '<div class="notice notice--info">系统自动识别到报价表；如果工作表或列不对，'
            '请到项目资料点“识别不对？手工选列”重新导入。</div>'
        )
    elif detected_by == "profile":
        source_notice = (
            '<div class="notice notice--success">✓ 本次使用了该客户上次保存的列映射，'
            '同类报价表会自动套用。</div>'
        )
    elif detected_by == "manual":
        source_notice = (
            '<div class="notice notice--success">✓ 已保存为该客户的列映射；'
            '下次导入同类报价表会自动套用，不需要再选列。</div>'
        )
    else:
        source_notice = ""
    body = f"""
{_history_back("返回上一页", f"/projects/{project.id}/custom")}
<div class="notice notice--success">✓ 模板价格已导入：{esc((book.get('counts') or {}).get('priced', 0))} 行可计价。同一文件再次“导入价格”会复用这份价格书，不会重复生成。</div>
{notice_html}
{source_notice}
<div class="page-head">
  <div>
    <p class="eyebrow">{esc(project.name)} · 模板价格书</p>
    <h1>{esc(price_book.get('name', ''))}</h1>
    <p class="lead">{esc(source_text)}</p>
  </div>
  <div class="page-actions">
    {('<span class="badge badge--ok">账号默认模板</span>' if is_account_default else
      f'<form method="post" action="/projects/{esc(project.id)}/price-books/{esc(price_book.get("id"))}/set-default" data-confirm="设为账号默认模板？以后新项目会自动准备这份模板和价格书；历史报价不变。"><button class="btn btn--small" type="submit">设为账号默认</button></form>')}
    <form method="post" action="/projects/{esc(project.id)}/price-books/{esc(price_book.get('id'))}/delete"
      data-confirm="确定删除整本价格书？草稿正在使用时会阻止删除；删除后可重新导入。">
      <button class="btn btn--ghost btn--small" type="submit">删除价格书</button>
    </form>
  </div>
</div>
<div class="kpis">
  <div class="kpi"><small>可计价行</small><strong>{esc((book.get('counts') or {}).get('priced', 0))}</strong></div>
  <div class="kpi"><small>设备行</small><strong>{esc((book.get('counts') or {}).get('equipment', 0))}</strong></div>
  <div class="kpi"><small>管线行</small><strong>{esc((book.get('counts') or {}).get('pipe', 0))}</strong></div>
</div>
<div class="card"><h2>导入提示</h2><ul class="excluded">{warnings or '<li>没有提示。</li>'}</ul></div>
{error_html}
<div class="card">
  <h2>项目价补项（仅本项目）</h2>
  <p class="muted">模板没有的设备/材料价，可以在这里补一行；只在本次项目价格书中使用，不写回母模板。适用型号别名用于自动匹配图纸型号。</p>
  <form method="post" action="/projects/{esc(project.id)}/price-books/{esc(price_book.get('id'))}/project-price" data-loading="正在添加项目价行…">
    <div class="form-grid">
      <label class="field"><span>名称 *</span><input name="name" placeholder="例如：15匹1拖3（项目价）" required></label>
      <label class="field"><span>单位</span><input name="unit" value="套"></label>
      <label class="field field--wide"><span>规格及型号</span><input name="spec" placeholder="例如：MDV-375W/SN1-8R1P + 3×MDV-125Q4/BPN1-DP"></label>
      <label class="field"><span>材料单价 *</span><input name="material" type="number" min="0" step="0.01" required></label>
      <label class="field"><span>安装单价 *</span><input name="installation" type="number" min="0" step="0.01" required></label>
      <label class="field"><span>分类</span>
        <select name="category"><option value="equipment">设备</option><option value="pipe">管线/材料</option></select></label>
      <label class="field"><span>系统</span>
        <select name="system"><option value="general">通用</option><option value="water">水系统</option><option value="refrigerant">冷媒系统</option></select></label>
      <label class="field field--wide"><span>适用型号/别名（逗号分隔）</span>
        <input name="aliases" placeholder="例如：MDV-D125Q4/SDN1-D, RMX112C"></label>
    </div>
    <div class="form-actions"><button class="btn btn--small" type="submit">添加项目价行</button></div>
  </form>
</div>
<div class="group-card">
  <div class="table-wrap"><table class="table">
    <thead><tr><th>行</th><th>序号</th><th>名称</th><th>型号/规格</th><th>单位</th><th>材料价</th><th>安装价</th><th>分类</th><th>状态</th><th>改价 / 删行</th></tr></thead>
    <tbody>{''.join(rows) or '<tr><td colspan="10">没有读到价格行</td></tr>'}</tbody>
  </table></div>
</div>
<div class="card">
  <h2>下一步</h2>
  <p class="muted">模板价格已经导入。接下来检查图纸；两项都完成后，回到“新图纸报价”勾选检查记录和这份价格书，创建草稿。</p>
  <div class="download-row">
    <a class="btn btn--small" href="/projects/{esc(project.id)}/files">去检查图纸</a>
    <a class="btn btn--small btn--ghost" href="/projects/{esc(project.id)}/custom">去创建报价草稿</a>
  </div>
</div>
"""
    return app_page(
        f"模板价格书 · {price_book.get('name', '')}",
        body,
        user,
        active="projects",
        project=project,
        crumbs=_crumb(
            (("我的项目", "/projects"), (project.name, f"/projects/{project.id}"), ("模板价格书", None))
        ),
    )


def compare_price_books_page(
    user: dict,
    project: Project,
    left: dict,
    right: dict,
) -> str:
    left_rows = {
        int(row["rowNumber"]): row
        for row in (left.get("book") or {}).get("rows", [])
    }
    right_rows = {
        int(row["rowNumber"]): row
        for row in (right.get("book") or {}).get("rows", [])
    }
    rows = []
    changed = 0
    for number in sorted(set(left_rows) | set(right_rows)):
        a = left_rows.get(number, {})
        b = right_rows.get(number, {})
        try:
            delta_material = Decimal(str(b.get("material") or 0)) - Decimal(
                str(a.get("material") or 0)
            )
            delta_install = Decimal(str(b.get("installation") or 0)) - Decimal(
                str(a.get("installation") or 0)
            )
        except (InvalidOperation, TypeError, ValueError):
            delta_material = Decimal("0")
            delta_install = Decimal("0")
        if delta_material or delta_install:
            changed += 1
        rows.append(
            "<tr>"
            f"<td>{number}</td>"
            f"<td>{esc(a.get('name') or b.get('name') or '')}</td>"
            f"<td>{esc(a.get('spec') or b.get('spec') or '')}</td>"
            f'<td class="num">{esc(a.get("material") or "—")}</td>'
            f'<td class="num">{esc(b.get("material") or "—")}</td>'
            f'<td class="num">{esc(f"{delta_material:,.2f}")}</td>'
            f'<td class="num">{esc(a.get("installation") or "—")}</td>'
            f'<td class="num">{esc(b.get("installation") or "—")}</td>'
            f'<td class="num">{esc(f"{delta_install:,.2f}")}</td>'
            "</tr>"
        )
    body = f"""
{_history_back("返回上一页", f"/projects/{project.id}/custom")}
<div class="page-head">
  <div>
    <p class="eyebrow">{esc(project.name)} · 价格对比</p>
    <h1>两套价格书对比</h1>
    <p class="lead">按模板行号对比材料价和安装价；只比较已导入的价格书，不声称是市场最新价。</p>
  </div>
</div>
<div class="quote-summary">
  <section class="card"><h2>{esc(left.get('name'))}</h2><p class="muted">{esc(left.get('fileName'))}</p></section>
  <section class="card"><h2>{esc(right.get('name'))}</h2><p class="muted">{esc(right.get('fileName'))}</p></section>
</div>
<div class="card"><h2>差异：{changed} 行</h2>
  <div class="table-wrap"><table class="table">
    <thead><tr><th>行</th><th>名称</th><th>规格</th><th>材料价 A</th><th>材料价 B</th><th>材料差</th><th>安装价 A</th><th>安装价 B</th><th>安装差</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table></div>
</div>
"""
    return app_page(
        "两套价格书对比 · HVAC Quote 工程报价",
        body,
        user,
        active="projects",
        project=project,
        crumbs=_crumb(
            (("我的项目", "/projects"), (project.name, f"/projects/{project.id}"), ("价格对比", None))
        ),
    )


def _copper_template_options(price_books: tuple[dict, ...]) -> str:
    options = ['<option value="">不对比模板材料价</option>']
    for book in price_books:
        for row in (book.get("book") or {}).get("rows", []):
            if row.get("priceState") != "priced":
                continue
            if row.get("category") != "pipe":
                continue
            text = f"{row.get('name', '')}{row.get('spec', '')}"
            if not any(marker in text for marker in ("铜管", "冷媒", "液管", "气管")):
                continue
            value = f"{book.get('id')}|{row.get('rowNumber')}"
            label = (
                f"{book.get('name', '')} · 第{row.get('rowNumber')}行 · "
                f"{row.get('name', '')} {row.get('spec', '')} · "
                f"材料价 {row.get('material')} 元/{row.get('unit') or '米'}"
            )
            options.append(
                f'<option value="{esc(value)}">{esc(label)}</option>'
            )
    return "".join(options)


def copper_price_page(
    user: dict,
    project: Project,
    price_books: tuple[dict, ...],
    records: tuple[dict, ...],
    *,
    error: str | None = None,
) -> str:
    today = datetime.now().astimezone().date().isoformat()
    template_options = _copper_template_options(price_books)
    record_rows = []
    for record in records:
        confirmed = record.get("status") == "confirmed"
        status = (
            f'<span class="badge badge--ok">已确认 v{esc(record.get("version"))}</span>'
            if confirmed
            else '<span class="badge badge--info">草稿</span>'
        )
        source = esc(record.get("sourceName") or "未填写来源")
        if record.get("sourceUrl"):
            source = (
                f'<a href="{esc(record.get("sourceUrl"))}" target="_blank" '
                f'rel="noopener noreferrer">{source}</a>'
            )
        comparison = "未选模板"
        if record.get("templateMaterialPrice") not in (None, ""):
            delta = str(record.get("deltaPerM") or "—")
            percent = record.get("deltaPercent")
            percent_text = f"{percent}%" if percent not in (None, "") else "—"
            comparison = (
                f"模板第{esc(record.get('templateRow'))}行 "
                f"{esc(record.get('templateMaterialPrice'))} 元/米<br>"
                f"<span class=\"muted\">差异 {esc(delta)} 元/米（{esc(percent_text)}）</span>"
            )
        actions = ""
        if not confirmed:
            actions = (
                '<div class="download-row" style="justify-content:flex-start;margin-top:6px">'
                f'<form method="post" action="/projects/{esc(project.id)}/copper-prices" '
                'data-confirm="确认这条参考价并形成新版本？确认不会修改模板或已生成报价。">'
                f'<input type="hidden" name="record_id" value="{esc(record.get("id"))}">'
                '<button class="btn btn--small" type="submit" name="action" value="confirm">确认</button>'
                "</form>"
                f'<form method="post" action="/projects/{esc(project.id)}/copper-prices">'
                f'<input type="hidden" name="record_id" value="{esc(record.get("id"))}">'
                '<button class="btn btn--small btn--ghost" type="submit" name="action" value="delete">删除</button>'
                "</form></div>"
            )
        else:
            actions = (
                f'<span class="muted">确认人 {esc(record.get("confirmedBy") or "—")} '
                f'· {esc(datetime_text(record.get("confirmedAt") or ""))}</span>'
            )
        record_rows.append(
            "<tr>"
            f"<td>{esc(record.get('recordDate'))}<br><span class='muted'>{esc(record.get('region'))}</span></td>"
            f"<td>{status}<br><span class='muted'>{esc(record.get('grade'))} "
            f"φ{esc(record.get('outerDiameterMm'))}×{esc(record.get('wallThicknessMm'))}</span></td>"
            f"<td><strong>{esc(record.get('pricePerM'))} 元/米</strong><br>"
            f"<span class='muted'>每米重量 {esc(record.get('weightKgPerM'))} kg</span></td>"
            f"<td><span class='muted'>{esc(record.get('formula'))}</span></td>"
            f"<td>{comparison}</td>"
            f"<td>{source}</td>"
            f"<td>{actions}</td>"
            "</tr>"
        )
    error_html = f'<div class="alert" role="alert">{esc(error)}</div>' if error else ""
    body = f"""
<a class="back-link" href="/projects/{esc(project.id)}">← 返回项目</a>
<div class="page-head">
  <div>
    <p class="eyebrow">{esc(project.name)} · 铜管参考价</p>
    <h1>铜管参考价记录</h1>
    <p class="lead">按公开铜价和加工费计算每米参考价，与模板材料价对比。参考价只做提示，不修改模板、草稿或已生成报价。</p>
  </div>
</div>
{_context_help(
    "怎么用？",
    "先查公开行情，填入铜价和加工费；系统按牌号、外径、壁厚算参考价。需要对比模板时选一行铜管模板。确认只形成参考价版本，正式报价仍用模板价。",
    "/help#result",
    "查看报价说明",
)}
{error_html}
<div class="card">
  <h2>记录一条参考价</h2>
  <p class="muted">示例：Mysteel 广东铜管行情每个工作日约 11 点更新；详情页价格可能需要权限。不要把股票铜价直接当成品铜管价，也不要把公开价当成客户实际拿货价。</p>
  <form method="post" action="/projects/{esc(project.id)}/copper-prices" data-loading="正在计算参考价…">
    <div class="form-grid">
      <label class="field"><span>日期</span><input name="record_date" type="date" value="{esc(today)}" required></label>
      <label class="field"><span>地区</span><input name="region" value="广东"></label>
      <label class="field"><span>来源名称</span><input name="source_name" value="Mysteel 我的钢铁网"></label>
      <label class="field"><span>来源链接</span><input name="source_url" value="https://tong.m.mysteel.com/tongguan/" placeholder="https://..."></label>
      <label class="field"><span>牌号</span>
        <select name="grade"><option value="TP2">TP2 紫铜管</option><option value="H62">H62 黄铜管</option><option value="H65">H65 黄铜管</option></select></label>
      <label class="field"><span>外径（mm）</span><input name="outer_diameter_mm" inputmode="decimal" placeholder="例如 9.52" required></label>
      <label class="field"><span>壁厚（mm）</span><input name="wall_thickness_mm" inputmode="decimal" placeholder="例如 0.8" required></label>
      <label class="field"><span>铜价（元/吨）</span><input name="copper_price_per_ton" inputmode="decimal" placeholder="例如 112900" required></label>
      <label class="field"><span>加工费（元/吨）</span><input name="processing_fee_per_ton" inputmode="decimal" placeholder="例如 3100" required></label>
      <label class="field field--wide"><span>对比模板材料价（可选）</span>
        <select name="template_ref">{template_options}</select></label>
      <label class="field field--wide"><span>备注</span><input name="note" placeholder="例如：公开行情仅供参考，未含运费"></label>
    </div>
    <p class="muted">计算：每米重量 = 牌号系数 × 壁厚 × (外径 − 壁厚)；参考价 = (铜价 + 加工费) ÷ 1000 × 每米重量。TP2 系数 0.02796，H62/H65 系数 0.02670。</p>
    <div class="form-actions"><button class="btn" type="submit" name="action" value="add">计算并保存</button></div>
  </form>
</div>
<div class="section-head"><h2>参考价记录</h2><span class="muted">{len(records)} 条</span></div>
<div class="card">
  <div class="table-wrap"><table class="table">
    <thead><tr><th>日期/地区</th><th>规格/状态</th><th>参考价</th><th>计算过程</th><th>模板对比</th><th>来源</th><th>操作</th></tr></thead>
    <tbody>{''.join(record_rows) or '<tr><td colspan="7">还没有参考价记录。</td></tr>'}</tbody>
  </table></div>
</div>
"""
    return app_page(
        f"铜管参考价 · {project.name}",
        body,
        user,
        active="projects",
        project=project,
        crumbs=_crumb(
            (("我的项目", "/projects"), (project.name, f"/projects/{project.id}"), ("铜管参考价", None))
        ),
    )


def custom_workspace_page(
    user: dict,
    project: Project,
    inspections: tuple[dict, ...],
    price_books: tuple[dict, ...],
    drafts: tuple[dict, ...],
    *,
    default_template_available: bool = False,
    account_default: dict | None = None,
) -> str:
    default_sha = str((account_default or {}).get("sha256") or "")
    default_book = next(
        (
            item
            for item in price_books
            if default_sha
            and str((item.get("book") or {}).get("sourceSha256") or "")
            == default_sha
        ),
        None,
    )
    default_notice = (
        '<div class="notice notice--info">账号默认模板：'
        f'{esc((account_default or {}).get("priceBookName") or (account_default or {}).get("fileName") or "")}；'
        "新项目已自动准备同一份受保护模板和价格书。"
        '<a href="/account">查看或更换</a></div>'
        if account_default
        else ""
    )
    inspection_html = "".join(
        f'<div class="file-row"><div><div class="file-name">'
        f'{esc(item.get("fileName", ""))}</div><div class="file-meta">'
        f'设备组 {esc((item.get("report") or {}).get("counts", {}).get("equipmentGroups", 0))} · '
        f'管线组 {esc((item.get("report") or {}).get("counts", {}).get("routeGroups", 0))} · '
        f'{esc(datetime_text(item.get("createdAt", "")))}</div></div>'
        f'<div class="file-size">{len((item.get("report") or {}).get("issues", []))} 个疑点</div>'
        f'<div class="file-actions"><a href="/projects/{esc(project.id)}/inspections/{esc(item["id"])}">查看检查</a></div></div>'
        for item in inspections
    ) or '<div class="file-empty">还没有图纸检查。先到项目资料上传 DWG/DXF，再点“检查图纸”。</div>'
    price_html = "".join(
        f'<div class="file-row"><div><div class="file-name">{esc(item.get("name", ""))}</div>'
        f'<div class="file-meta">{esc(item.get("fileName", ""))} · '
        f'{esc(datetime_text(item.get("createdAt", "")))}</div></div>'
        f'<div class="file-size">{esc((item.get("book") or {}).get("counts", {}).get("priced", 0))} 行可计价</div>'
        f'<div class="file-actions"><a href="/projects/{esc(project.id)}/price-books/{esc(item["id"])}">查看价格书</a></div></div>'
        for item in price_books
    ) or '<div class="file-empty">还没有模板价格书。先到项目资料上传 XLSX 模板，再点“导入价格”。</div>'
    draft_html = "".join(
        f'<div class="file-row"><div><div class="file-name">{esc((item.get("data") or {}).get("title") or "新图纸报价")}</div>'
        f'<div class="file-meta">{esc(item.get("drawingName", ""))} · {esc(item.get("priceBookName") or "未选价格书")} · {esc(datetime_text(item.get("updatedAt", "")))}</div></div>'
        f'<div class="file-size">{esc("已生成" if item.get("status") == "ready" else "草稿")}</div>'
        f'<div class="file-actions"><a href="/projects/{esc(project.id)}/drafts/{esc(item["id"])}">打开</a>'
        f'<form method="post" action="/projects/{esc(project.id)}/drafts/{esc(item["id"])}/copy" style="display:inline">'
        f'<button class="text-button" type="submit">复制</button></form>'
        f'<form method="post" action="/projects/{esc(project.id)}/drafts/{esc(item["id"])}/delete"'
        f' data-confirm="确定删除这份草稿？删除后无法恢复。" style="display:inline">'
        f'<input type="hidden" name="expected_revision" value="{esc(item.get("revision") or 1)}">'
        f'<button class="text-button" type="submit">删除</button></form></div></div>'
        for item in drafts
    ) or '<div class="file-empty">还没有新图纸草稿。</div>'
    if inspections and price_books:
        inspection_options = "".join(
            '<div class="check-line" style="justify-content:space-between;gap:12px">'
            f'<input type="checkbox" name="inspection_ids" value="{esc(item["id"])}"> '
            f'<span style="flex:1">{esc(item.get("fileName", ""))}'
            f'<span class="muted"> · 设备组 {esc((item.get("report") or {}).get("counts", {}).get("equipmentGroups", 0))}'
            f" · 管线组 {esc((item.get('report') or {}).get('counts', {}).get('routeGroups', 0))}</span></span>"
            f'<label class="check-line" style="flex:none">×'
            f'<input type="number" name="multiplier_{esc(item["id"])}" min="1" max="20" step="1" value="1" style="width:56px"></label>'
            "</div>"
            for item in inspections
        )
        book_options = "".join(
            f'<option value="{esc(item["id"])}"'
            f'{" selected" if default_book and item["id"] == default_book["id"] else ""}>'
            f'{esc(item.get("name", ""))}'
            f'{"（账号默认）" if default_book and item["id"] == default_book["id"] else ""}'
            "</option>"
            for item in price_books
        )
        create_form = f"""
<form method="post" action="/projects/{esc(project.id)}/custom/drafts" data-loading="正在创建报价草稿…">
  <div class="form-grid">
    <div class="field field--wide"><span>图纸检查（可勾选多张合并为一份报价）*</span>
      <div class="check-list">{inspection_options}</div>
    </div>
    <label class="field"><span>模板价格书 *</span><select name="price_book_id">{book_options}</select></label>
    <label class="field field--wide"><span>草稿名称</span><input name="title" placeholder="例如：XX项目 3 层新图纸报价"></label>
  </div>
  <p class="muted">勾选多张图纸时，系统按各图纸比例分别计算长度后合并；需要只报某一层时，请到该图纸检查页选择“本次范围”。</p>
  <div class="form-actions">
    <button class="btn" type="submit">创建报价草稿</button>
    {('<button class="btn btn--ghost" type="submit" name="use_account_default" value="1">用账号默认模板创建</button>' if default_book else '')}
  </div>
</form>"""
    else:
        default_button = ""
        if default_template_available and not price_books:
            default_button = (
                f'<form method="post" action="/projects/{esc(project.id)}/custom/default-template" '
                'data-loading="正在导入系统默认模板…">'
                '<button class="btn btn--small" type="submit">使用系统默认模板</button></form>'
            )
        hint = (
            "需要至少一份图纸检查和一份模板价格书，才能创建报价草稿。"
            if default_template_available
            else "需要至少一份图纸检查和一份模板价格书，才能创建报价草稿；请先在项目资料上传客户模板。"
        )
        create_form = f'<div class="notice">{hint}{default_button}</div>'
    drawing_count = sum(1 for item in project.files if item.kind == "drawing")
    template_count = sum(1 for item in project.files if item.kind == "template")
    has_inspection = bool(inspections)
    has_price = bool(price_books)
    can_create = has_inspection and has_price
    body = f"""
{_history_back("返回上一页", f"/projects/{project.id}")}
<div class="page-head">
  <div>
    <p class="eyebrow">{esc(project.name)}</p>
    <h1>新图纸报价</h1>
    <p class="lead">先检查图纸、导入模板价格，再由你确认候选设备和管线；系统不会把未确认的候选直接写成工程量。</p>
  </div>
  <div class="page-actions">
    {f'<a class="btn btn--ghost" href="/projects/{esc(project.id)}/price-books/compare?left={esc(price_books[0]["id"])}&right={esc(price_books[1]["id"])}">对比价格书</a>' if len(price_books) >= 2 else ''}
    <a class="btn btn--ghost" href="/projects/{esc(project.id)}/copper-prices">铜管参考价</a>
    <a class="btn" href="/projects/{esc(project.id)}/files">上传 / 检查资料</a>
  </div>
</div>
<section class="workflow" aria-label="报价流程">
  <div class="workflow-head">
    <div>
      <h2>报价流程</h2>
      <p>上传资料后，还要完成“检查图纸”和“导入价格”；两项都完成后，创建草稿才会解锁。</p>
    </div>
    <a class="help-link" href="/help#project-files">查看完整说明 →</a>
  </div>
  <ol class="workflow-steps">
    <li class="workflow-step {'done' if project.files else 'active'}">
      <span class="num">1</span><strong>上传图纸和模板</strong>
      <small>{drawing_count} 份图纸 · {template_count} 份模板</small>
      <a href="/projects/{esc(project.id)}/files">管理项目资料 →</a>
    </li>
    <li class="workflow-step {'done' if has_inspection else 'active'}">
      <span class="num">2</span><strong>检查图纸</strong>
      <small>{len(inspections)} 份检查记录，含候选设备、管线和疑点</small>
      <a href="/projects/{esc(project.id)}/files">去检查图纸 →</a>
    </li>
    <li class="workflow-step {'done' if has_price else 'active'}">
      <span class="num">3</span><strong>导入模板价格</strong>
      <small>{len(price_books)} 份价格书，按识别并确认的列映射读取单价</small>
      <a href="/projects/{esc(project.id)}/files">去导入价格 →</a>
    </li>
    <li class="workflow-step {'active' if can_create else ''}">
      <span class="num">4</span><strong>创建草稿并生成</strong>
      <small>{len(drafts)} 份草稿；逐组确认后才生成 Excel/PDF</small>
      <a href="#create-draft">{"去创建草稿 →" if can_create else "完成前两步后解锁"}</a>
    </li>
  </ol>
</section>
{_context_help(
    "新图纸怎么走？",
    "上传 DWG/DXF 和 XLSX 模板 → 检查图纸 → 导入价格 → 创建草稿 → 逐组确认模板行和数量 → 生成报价。",
    "/help#project-files",
    "查看资料说明",
)}
{default_notice}
<div class="quote-summary">
  <section class="card"><h2>2. 图纸检查</h2><p class="muted">按图块/图层生成候选设备与管线，并列出比例、型号、长度疑点。</p>{inspection_html}</section>
  <section class="card"><h2>3. 模板价格</h2><p class="muted">按确认的工作表和列映射读取价格；公式价没有缓存值时标为待重算。</p>{price_html}</section>
</div>
<div class="card" id="create-draft"><h2>4. 创建草稿</h2>{create_form}</div>
<div class="section-head"><h2>报价草稿</h2><span class="muted">{len(drafts)} 份</span></div>
<div class="card">{draft_html}</div>
"""
    return app_page(
        "新图纸报价 · HVAC Quote 工程报价",
        body,
        user,
        active="projects",
        project=project,
        crumbs=_crumb(
            (("我的项目", "/projects"), (project.name, f"/projects/{project.id}"), ("新图纸报价", None))
        ),
    )


def draft_page(
    user: dict,
    project: Project,
    draft: dict,
    price_book: dict,
    exceptions: tuple[dict, ...],
    *,
    price_books: tuple[dict, ...] = (),
    error: str | None = None,
    publication_key: str = "",
    vision: dict | None = None,
) -> str:
    data = draft.get("data") or {}
    priced_rows = {
        int(row["rowNumber"]): row
        for row in (price_book.get("book") or {}).get("rows", [])
        if row.get("priceState") == "priced"
    }

    def has_usable_row(group: dict) -> bool:
        try:
            return int(group.get("priceRow") or 0) in priced_rows
        except (TypeError, ValueError):
            return False

    def needs_customer_price(group: dict) -> bool:
        return bool(
            group.get("isWater")
            or group.get("manualMaterial") not in (None, "")
            or group.get("manualInstallation") not in (None, "")
            or not has_usable_row(group)
        )

    equipment_rows = []
    for index, group in enumerate(data.get("equipment", [])):
        auto_component = bool(group.get("isComponent"))
        manual_component = bool(group.get("isComponentManual"))
        component_checked = auto_component or manual_component
        include_checked = bool(group.get("include")) and not component_checked
        notes = []
        if auto_component:
            notes.append("已自动并入成套，不单独计价")
        if manual_component:
            notes.append("人工标记为套内组成，不单独计价")
        if group.get("isSet"):
            notes.append("按套计价")
        if group.get("setIncomplete"):
            notes.append("模板按套计价，请补齐成套数量或改选单机行")
        if group.get("modelConflict"):
            notes.append("图面标注与图块属性不一致")
        if group.get("procurementModelPending") or group.get("modelSource") == "block":
            if group.get("manualLabel"):
                notes.append("采购名称/型号已人工确认")
            elif has_usable_row(group):
                notes.append("图纸采购型号待核；报价采用所选模板型号，请核对")
            else:
                notes.append("采购型号待核：仅图块名称；请选择确认的模板型号或填写确认型号及客户补价")
        if group.get("manualMaterial") not in (None, "") or group.get(
            "manualInstallation"
        ) not in (None, ""):
            notes.append("按客户补价计价")
        note_html = (
            f'<br><span class="badge badge--warn">{esc("；".join(notes))}</span>'
            if notes
            else ""
        )
        equipment_meta = " · ".join(
            part
            for part in (
                _display_view(group.get("floorLabel")),
                _display_layer(group.get("layer"))
                if str(group.get("layer") or "") not in {"", "0"}
                else "",
            )
            if part
        )
        component_input = (
            '<input type="checkbox" checked disabled>'
            if auto_component
            else f'<input type="checkbox" name="equipment_component_{index}" '
            f'{"checked" if manual_component else ""}>'
        )
        process_items = []
        if group.get("isSet"):
            process_items.append('<span class="badge badge--ok">成套</span>')
        elif not group.get("setIncomplete") or group.get("componentKind") == "indoor":
            label = "套内" if not auto_component else "已并入"
            process_items.append(
                f'<label title="仅作套内组成，不单独计价">{component_input}{label}</label>'
            )
        if group.get("setIncomplete") or (
            group.get("unit") == "套"
            and not group.get("isSet")
            and group.get("componentKind") != "indoor"
        ):
            process_items.append(
                f'<label title="确认按套计价">'
                f'<input type="checkbox" name="equipment_set_confirm_{index}" '
                f'{"checked" if group.get("setConfirmed") else ""}>按套</label>'
            )
        process_cell = f'<div class="stack-check">{"".join(process_items)}</div>'
        manual_inputs = ""
        if needs_customer_price(group):
            label_text = "水系统人工价" if group.get("isWater") else "客户补价"
            label_input = ""
            if not has_usable_row(group):
                label_input = (
                    f'<input name="equipment_manual_label_{index}" '
                    f'value="{esc(group.get("manualLabel") or "")}" '
                    'placeholder="确认名称/型号" style="width:132px">'
                )
            manual_inputs = (
                f'<div class="water-inputs" title="模板没有对应行或客户另有确认价时填写；'
                f'填写后按客户价计价，不写回模板">'
                f"<span>{label_text}</span>{label_input}"
                f'<input name="equipment_manual_material_{index}" inputmode="decimal" '
                f'value="{esc(group.get("manualMaterial") or "")}" placeholder="材料价">'
                f'<input name="equipment_manual_installation_{index}" inputmode="decimal" '
                f'value="{esc(group.get("manualInstallation") or "")}" placeholder="安装价">'
                f'<input class="water-unit" name="equipment_manual_unit_{index}" '
                f'value="{esc(group.get("manualUnit") or "台")}" placeholder="单位">'
                "</div>"
            )
        equipment_rows.append(
            f'<tr class="{"muted-row" if component_checked else ""}">'
            f'<td class="check-cell"><input type="checkbox" name="equipment_include_{index}" '
            f'{"checked" if include_checked else ""} '
            f'{"disabled" if component_checked else ""}></td>'
            f"<td>{esc(group.get('manualLabel') or group.get('label'))}<br><span class='muted'>"
            f"{esc(equipment_meta)}</span>{note_html}{manual_inputs}</td>"
            f'<td><input type="number" min="0" step="1" name="equipment_quantity_{index}" '
            f'value="{esc(group.get("quantity", 0))}"></td>'
            f'<td><select name="equipment_price_{index}">'
            f'{_price_options(price_book, group.get("priceRow"), "equipment")}</select></td>'
            f"<td>{esc(_confidence_label(str(group.get('confidence'))))}</td>"
            f"<td>{process_cell}</td>"
            "</tr>"
        )
    route_rows = []
    for index, group in enumerate(data.get("routes", [])):
        linked = group.get("linkedMaterial") or {}
        if linked:
            linked_text = (
                f"第{esc(linked.get('priceRow'))}行 {esc(linked.get('name'))} "
                f"{esc(linked.get('spec'))}（等长）"
            )
        elif group.get("linkedMaterialMissing"):
            linked_text = '<span class="badge badge--warn">模板缺对应保温行</span>'
        else:
            linked_text = "—"
        spec_text = " ".join(
            part
            for part in (group.get("manualSpec") or group.get("spec"), group.get("role"))
            if part
        ) or "规格未确定"
        route_notes = [
            _issue_text(str(issue)) for issue in group.get("issues") or []
        ]
        if (
            group.get("lengthM") in (None, "")
            or group.get("lengthStatus") in {"partial", "unmeasured"}
        ):
            pending_note = "长度待核"
            known_length = group.get("lengthKnownM")
            unmeasured = int(group.get("unmeasuredSegmentCount") or 0)
            if known_length not in (None, "") and unmeasured:
                pending_note += (
                    f"（已知小计 {format_quantity(known_length)} 米，"
                    f"{unmeasured} 段未测）"
                )
            route_notes.append(pending_note)
        if group.get("manualSpec"):
            route_notes.append("规格由客户确认")
        if group.get("manualMaterial") not in (None, "") or group.get(
            "manualInstallation"
        ) not in (None, ""):
            route_notes.append("按客户补价计价")
        route_note = (
            f'<br><span class="muted">{esc(" / ".join(route_notes))}</span>'
            if route_notes
            else ""
        )
        boundary_rows = []
        seen_boundaries = set()
        for segment in group.get("segments") or []:
            boundary = segment.get("boundaryEvidence") or {}
            symbol = str(boundary.get("symbolHandle") or "")
            if not symbol or symbol in seen_boundaries:
                continue
            seen_boundaries.add(symbol)
            source_id = str(group.get("sourceInspectionId") or data.get("inspectionId") or "")
            scale = (data.get("sourceScales") or {}).get(source_id) or data.get("scale")
            interval = boundary.get("apexBaseLengthEnvelopeRaw") or []
            interval_text = ""
            try:
                if len(interval) == 2 and scale:
                    width = abs(Decimal(str(interval[1])) - Decimal(str(interval[0]))) * Decimal(str(scale))
                    interval_text = f"两口径之间的原图局部区间 {width:.6f} 米。"
            except (InvalidOperation, TypeError, ValueError):
                pass
            boundary_rows.append(
                "<li>当前分段提案按原图分歧符号尖端投影；另保留底边投影口径。"
                f"{esc(interval_text)}请确认本工程计量口径后勾选路线核对；"
                "分段只改变规格归属，原路线总长与液管总长不变。</li>"
            )
        boundary_html = (
            '<details class="muted"><summary>分歧边界计量依据（需确认口径）</summary>'
            f'<ul>{"".join(boundary_rows)}</ul></details>'
            if boundary_rows else ""
        )
        spec_input = ""
        route_issues = [str(issue) for issue in group.get("issues") or []]
        if (
            not str(group.get("spec") or "").strip()
            or group.get("specSource") in {"layer_dimension", "customer"}
            or {"PIPE_SPEC_UNRESOLVED", "PIPE_SPEC_AMBIGUOUS"} & set(route_issues)
        ):
            spec_input = (
                '<div class="water-inputs" title="客户可补填图上没有读到的管径/规格；'
                '套价仍需选择模板行或填写客户补价">'
                f"<span>确认管径/规格</span>"
                f'<input name="route_manual_spec_{index}" '
                f'value="{esc(group.get("manualSpec") or "")}" '
                'placeholder="例如 DN100 / 保温" style="width:150px">'
                "</div>"
            )
        manual_inputs = ""
        if needs_customer_price(group):
            label_text = "水系统人工价" if group.get("isWater") else "客户补价"
            manual_inputs = (
                f'<div class="water-inputs" title="模板没有对应行或客户另有确认价时填写；'
                f'填写后按客户价计价，不写回模板">'
                f"<span>{label_text}</span>"
                f'<input name="route_manual_material_{index}" inputmode="decimal" '
                f'value="{esc(group.get("manualMaterial") or "")}" placeholder="材料价">'
                f'<input name="route_manual_installation_{index}" inputmode="decimal" '
                f'value="{esc(group.get("manualInstallation") or "")}" placeholder="安装价">'
                f'<input class="water-unit" name="route_manual_unit_{index}" '
                f'value="{esc(group.get("manualUnit") or "米")}" placeholder="单位">'
                "</div>"
            )
        review_confirm = ""
        if group.get("eligibility") == "review" or group.get("reviewReasons"):
            reviewed = bool((group.get("routeReview") or {}).get("confirmed"))
            review_confirm = (
                f'<label class="muted" style="display:block;margin-top:4px">'
                f'<input type="checkbox" name="route_review_confirm_{index}" '
                f'{"checked" if reviewed else ""}> '
                "已按图核对本组路线、规格和范围</label>"
            )
        route_rows.append(
            "<tr>"
            f'<td class="check-cell"><input type="checkbox" name="route_include_{index}" '
            f'{"checked" if group.get("include") else ""}></td>'
            f"<td>{esc(_display_layer(group.get('label')))}<br><span class='muted'>"
            f"{esc(_display_view(group.get('floorLabel')))} · {esc(group.get('category'))} · "
            f"{esc(spec_text)}</span>{route_note}{boundary_html}{spec_input}{manual_inputs}"
            f"{review_confirm}</td>"
            f'<td><input class="route-length" data-raw="{esc(group.get("lengthRaw") or "")}" '
            f'type="number" min="0" step="any" name="route_length_{index}" '
            f'placeholder="{"长度待核" if group.get("lengthM") in (None, "") else ""}" '
            f'value="{esc(group.get("lengthM") if group.get("lengthM") is not None else "")}"></td>'
            f'<td><select name="route_price_{index}">'
            f'{_price_options(price_book, group.get("priceRow"), "pipe")}</select></td>'
            f"<td>{esc(_confidence_label(str(group.get('confidence'))))}</td>"
            f"<td>{linked_text}</td>"
            "</tr>"
        )
    exception_html = "".join(
        f"<li>{_issue_badge(str(item.get('severity', 'review')), bool(item.get('acknowledged')))} "
        f"{esc(item.get('message', ''))}</li>"
        for item in exceptions
    ) or "<li>没有未处理的异常。</li>"
    error_html = f'<div class="alert" role="alert">{esc(error)}</div>' if error else ""
    source_files = data.get("sourceFiles") or []
    source_text = "、".join(
        str(item.get("fileName") or "") for item in source_files
    ) or str(draft.get("drawingName") or "")
    source_html = "".join(
        f'<li>{esc(item.get("fileName") or "")} · {esc(_display_view(item.get("view")))}</li>'
        for item in source_files
    )
    current_multipliers = data.get("sourceMultipliers") or {}
    multiplier_sources = source_files or [
        {
            "inspectionId": draft.get("inspectionId"),
            "fileName": source_text or "本图纸",
        }
    ]
    multiplier_rows = "".join(
        f'<label class="field"><span>{esc(item.get("fileName") or "本图纸")} 代表层数</span>'
        f'<input name="source_multiplier_{esc(item.get("inspectionId") or "")}" '
        f'type="number" min="1" max="20" step="1" '
        f'value="{esc(current_multipliers.get(str(item.get("inspectionId") or ""), "1"))}"></label>'
        for item in multiplier_sources
    )

    def source_scale_value(source_id: str) -> str:
        stored = (data.get("sourceScales") or {}).get(source_id)
        if stored not in (None, ""):
            return str(stored)
        for group in data.get("routes") or []:
            if str(group.get("sourceInspectionId") or "") != source_id:
                continue
            try:
                raw = Decimal(str(group.get("lengthRaw")))
                length = Decimal(str(group.get("lengthM")))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if raw > 0 and length > 0:
                return format((length / raw).normalize(), "f")
        return ""

    if data.get("scaleEditable", True):
        scale_summary = f"{data.get('scale') or '未确认'} 米/图面单位"
    else:
        scale_summary = "；".join(
            f"{item.get('fileName') or '本图纸'} "
            f"{source_scale_value(str(item.get('inspectionId') or '')) or '未确认'}"
            for item in multiplier_sources
        )

    blocking_issue_rows = []
    review_issue_rows = []
    issue_index: dict[tuple[str, str], int] = {}
    for index, item in enumerate(data.get("issues") or []):
        issue_index[
            (str(item.get("code") or ""), str(item.get("message") or ""))
        ] = index
        severity = str(item.get("severity") or "review")
        acknowledged = bool(item.get("acknowledged"))
        if severity == "blocks_pricing":
            ack_cell = (
                f'<label class="check-line" style="flex:none">'
                f'<input type="checkbox" name="ack_issue_{index}" '
                f'{"checked" if acknowledged else ""}>已核对，继续</label>'
            )
        else:
            ack_cell = '<span class="muted">提示，不阻止</span>'
        row = (
            f'<li id="drawing-issue-{index}">'
            f"{_issue_badge(severity, acknowledged)} "
            f"{esc(item.get('message', ''))} "
            f"<span style=\"display:inline-block;margin-left:8px\">{ack_cell}</span>"
            "</li>"
        )
        if severity == "blocks_pricing":
            blocking_issue_rows.append(row)
        else:
            review_issue_rows.append(row)
    drawing_issue_html = "".join(blocking_issue_rows) or (
        "<li>没有需要逐条确认的阻塞疑点。</li>"
    )
    drawing_review_html = (
        '<details class="issue-details">'
        f"<summary>另有 {len(review_issue_rows)} 条待核对提示（不阻止生成）</summary>"
        f'<ul class="excluded">{"".join(review_issue_rows)}</ul>'
        "</details>"
        if review_issue_rows
        else ""
    )

    def issue_anchor(item: dict) -> str:
        code = str(item.get("code") or "")
        if code.startswith(("EQUIPMENT", "SET_")):
            return "#equipment"
        if code.startswith(("PIPE", "LINKED_INSULATION")):
            return "#routes"
        if code.startswith("SCALE_"):
            return "#scale"
        if code.startswith("CUSTOM_ITEM"):
            return "#custom-items"
        if code.startswith("COEFFICIENT"):
            return "#coefficients"
        index = issue_index.get((code, str(item.get("message") or "")))
        return f"#drawing-issue-{index}" if index is not None else "#drawing-issues"

    blocked_open = [
        item
        for item in exceptions
        if item.get("severity") == "blocks_pricing"
        and not item.get("acknowledged")
    ]
    review_open = [
        item
        for item in exceptions
        if item.get("severity") != "blocks_pricing"
        and not item.get("acknowledged")
    ]
    todo_rows = "".join(
        f'<li><a href="{issue_anchor(item)}">'
        f'{esc(item.get("message", ""))}</a> '
        f'{_issue_badge(str(item.get("severity", "review")))}</li>'
        for item in blocked_open[:8]
    )
    more_blocked = (
        f'<li class="muted">还有 {len(blocked_open) - 8} 条阻塞项，见页面末尾异常清单。</li>'
        if len(blocked_open) > 8
        else ""
    )
    todo_html = f"""
<div class="card">
  <h2>还要补什么</h2>
  <p class="muted">只列会影响本次报价的资料。点条目可跳到对应填写位置；没有的型号/规格可在该行直接填客户补价，或到下方“补录未识别内容”添加。</p>
  {f'<ul class="excluded">{todo_rows}{more_blocked}</ul>' if blocked_open else '<div class="notice notice--info">当前没有必须补充的资料，可以直接生成；未识别的部分不会自动计入。</div>'}
  {f'<p class="muted">另有 {len(review_open)} 条待核对提示，不阻止生成，见页面末尾异常清单。</p>' if review_open else ''}
</div>"""

    def _length(value) -> Decimal:
        try:
            return Decimal(str(value or "0"))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0")

    equipment_all = list(data.get("equipment") or [])
    equipment_included = [
        group
        for group in equipment_all
        if group.get("include")
        and not (group.get("isComponent") or group.get("isComponentManual"))
    ]
    equipment_candidates = [
        group
        for group in equipment_all
        if not (group.get("isComponent") or group.get("isComponentManual"))
    ]
    pipe_all = list(data.get("routes") or [])
    pipe_candidates = list(pipe_all)
    pipe_included = [group for group in pipe_all if group.get("include")]
    pipe_total_length = sum(
        (_length(group.get("lengthM")) for group in pipe_included),
        Decimal("0"),
    )
    pipe_category_lengths: dict[str, Decimal] = {}
    uncertain_length = Decimal("0")
    for group in pipe_included:
        category = str(group.get("category") or "其他")
        length = _length(group.get("lengthM"))
        pipe_category_lengths[category] = (
            pipe_category_lengths.get(category, Decimal("0")) + length
        )
        if group.get("layerCertain") is False:
            uncertain_length += length
    unresolved = [
        group
        for group in pipe_all
        if not group.get("include")
        and "PIPE_SPEC_UNRESOLVED" in (group.get("issues") or [])
    ]
    unresolved_length = sum(
        (_length(group.get("lengthM")) for group in unresolved),
        Decimal("0"),
    )
    inferred = [
        group
        for group in pipe_all
        if group.get("specSource") == "layer_dimension"
        and not group.get("include")
    ]
    inferred_length = sum(
        (_length(group.get("lengthM")) for group in inferred),
        Decimal("0"),
    )
    incomplete_sets = [
        group
        for group in equipment_included
        if group.get("setConfirmed") and group.get("setIncomplete")
    ]
    category_text = " · ".join(
        f"{category} {format_quantity(length)} 米"
        for category, length in sorted(
            pipe_category_lengths.items(), key=lambda item: item[1], reverse=True
        )
    ) or "本次没有计入管线"
    recon_notices = []
    if incomplete_sets:
        recon_notices.append(
            '<div class="notice notice--danger">'
            f"有 {len(incomplete_sets)} 组设备勾了“按套确认”，但识别数量与模板成套"
            "数量不一致。生成会被阻止，请改套数或把室内机单独选单机价格行。"
            "</div>"
        )
    if uncertain_length > 0:
        recon_notices.append(
            '<div class="notice">'
            f"已计入的管线中有 {format_quantity(uncertain_length)} 米来自"
            "“图层不确定”的候选；请确认没有把其他专业管线算进来。"
            "</div>"
        )
    if unresolved:
        recon_notices.append(
            '<div class="notice notice--info">'
            f"有 {len(unresolved)} 组管线因规格未确定未计入，合计 "
            f"{format_quantity(unresolved_length)} 米；需要补规格后再报价。"
            "</div>"
        )
    if inferred:
        recon_notices.append(
            '<div class="notice notice--info">'
            f"有 {len(inferred)} 组管线按同系统 DN 标注推断（合计 "
            f"{format_quantity(inferred_length)} 米），已匹配模板行但默认未计入；"
            "请在下方管线组逐组勾选确认。"
            "</div>"
        )
    if not pipe_included and not equipment_included:
        recon_notices.append(
            '<div class="notice notice--info">本次尚未计入任何设备或管线；'
            "请确认图纸已检查并勾选了要报价的项目。</div>"
        )
    elif not pipe_included:
        recon_notices.append(
            '<div class="notice notice--info">本次没有计入管线，只报设备。</div>'
        )
    elif not equipment_included:
        recon_notices.append(
            '<div class="notice notice--info">本次没有计入设备，只报管线。</div>'
        )
    recon_html = f"""
<div class="card recon-card">
  <h2>报价前对账</h2>
  <p class="muted">先看“候选”和“已计入”是否对得上，再看管线总长和未计价部分。</p>
  <div class="recon-grid">
    <div class="kpi"><small>设备候选 / 已计入</small><strong>{len(equipment_candidates)} / {len(equipment_included)}</strong></div>
    <div class="kpi"><small>管线候选 / 已计入</small><strong>{len(pipe_candidates)} / {len(pipe_included)}</strong></div>
    <div class="kpi"><small>已计入管线长度</small><strong>{format_quantity(pipe_total_length)} 米</strong></div>
    <div class="kpi"><small>未计价管线</small><strong>{len(unresolved)} 组</strong></div>
  </div>
  <p class="muted">{esc(category_text)}</p>
    {''.join(recon_notices)}
</div>"""
    vision = vision or {}
    vision_enabled = bool(vision.get("enabled"))
    vision_config = vision.get("config") or {}
    vision_tasks = vision.get("tasks") or []
    vision_adoptions = vision.get("adoptions") or []
    preview = data.get("visionPreview") or {}
    preview_html = ""
    if preview and preview.get("requiresConfirmation"):
        pricing = preview.get("pricing") or {}
        change_rows = "".join(
            "<tr>"
            f"<td>{'设备' if item.get('kind') == 'equipment' else '管线'}</td>"
            f"<td>{esc((item.get('after') or {}).get('model') or (item.get('after') or {}).get('spec') or (item.get('after') or {}).get('medium') or '')}</td>"
            f"<td>{esc((item.get('before') or {}).get('include'))}</td>"
            f"<td>{esc((item.get('after') or {}).get('include'))}</td>"
            f"<td>{esc((item.get('after') or {}).get('lengthM') or '')}</td>"
            "</tr>"
            for item in preview.get("changed") or []
        )
        preview_html = f"""
<div class="notice notice--info">
  <strong>视觉采纳差异待确认。</strong>
  接受后按原生 CAD 重算，模板价格来自当前价格书。
  采纳前：{esc(pricing.get("beforeTotal") or "待重算")} 元 →
  采纳后预览：{esc(pricing.get("afterTotal") or "待重算")} 元
  {f'（差额 {esc(pricing.get("deltaTotal"))} 元）' if pricing.get("deltaTotal") else ''}
  {f'（采纳后预览失败：{esc(pricing.get("error"))}）' if pricing.get("error") else ''}
  {f'（采纳前预览失败：{esc(pricing.get("beforeError"))}）' if pricing.get("beforeError") else ''}。
  请在下方勾选“确认视觉差异并生成报价”，再点“生成报价”。
</div>
<div class="table-wrap"><table class="table">
  <thead><tr><th>类型</th><th>采用后</th><th>原计入</th><th>现计入</th><th>长度(米)</th></tr></thead>
  <tbody>{change_rows}</tbody>
</table></div>"""
    task_rows = "".join(
        '<div class="file-row"><div><div class="file-name">'
        f'{esc(item.get("status"))}</div><div class="file-meta">'
        f'任务 {esc(item.get("id"))} · {esc(item.get("createdAt"))}</div></div>'
        f'<div class="file-actions"><a href="/projects/{esc(project.id)}/vision-tasks/{esc(item.get("id"))}">查看</a></div></div>'
        for item in vision_tasks
    ) or '<p class="muted">还没有视觉任务。</p>'
    adoption_rows = "".join(
        f'<div class="file-row"><div><div class="file-name">采纳 {esc(item.get("id"))}</div>'
        f'<div class="file-meta">{esc(item.get("createdAt"))} · 版本 {esc(item.get("afterRevision"))}</div></div>'
        f'<div class="file-actions"><form method="post" action="/projects/{esc(project.id)}/drafts/{esc(draft["id"])}/vision-adoptions/{esc(item["id"])}/revoke"'
        ' data-confirm="撤销这次视觉采纳？只在草稿未被后续修改时允许；历史报价不变。">'
        f'<input type="hidden" name="expected_revision" value="{esc(draft.get("revision") or 1)}">'
        '<button class="text-button" type="submit">撤销</button></form></div></div>'
        for item in vision_adoptions
        if item.get("status") == "active"
    )
    if not vision_enabled:
        vision_html = (
            '<div class="card" id="vision"><h2>视觉辅助（试验）</h2>'
            '<p class="muted">当前关闭。关闭时普通 CAD 检查、人工确认和报价完整可用。'
            '开启后仍需选中具体局部并预览，系统不会自动发送整图。</p>'
            '<a class="btn btn--small btn--ghost" href="/account">到账号设置开启</a></div>'
        )
    else:
        inspection_id = str(data.get("inspectionId") or draft.get("inspectionId") or "")
        vision_html = f"""
<div class="card" id="vision">
  <h2>视觉辅助（试验）</h2>
  <p class="muted">模型：{esc(vision_config.get("model") or "未配置")} · 状态：
  {'可用' if vision_config.get("available") else '未配置或不可用'}。
  仅对选中局部调用；数量/长度由 CAD 重算，价格由模板计算。</p>
  {f'<p><a class="btn btn--small" href="/projects/{esc(project.id)}/inspections/{esc(inspection_id)}#vision">选择疑难局部并预览</a></p>' if inspection_id else ''}
  {preview_html}
  <h3>本次任务</h3>
  {task_rows}
  <h3>已生效的视觉采纳</h3>
  {adoption_rows or '<p class="muted">没有生效中的采纳记录。</p>'}
  <p class="muted">建议必须逐条接受/拒绝/待核；拒绝和待核不改报价。撤销只撤销指定采纳，历史已生成报价不改写。</p>
</div>"""
    bulk_html = f"""
<div class="card">
  <h2>批量补充（可选）</h2>
  <p class="muted">设备、管线、安装项可以一次填写；模板行和人工价二选一。整批先校验，有错误就全部不写入。</p>
  <div class="download-row">
    <a class="btn btn--small btn--ghost" href="/projects/{esc(project.id)}/drafts/{esc(draft['id'])}/supplement-template.xlsx">下载批量补充表</a>
    <a class="btn btn--small btn--ghost" href="/projects/{esc(project.id)}/drafts/{esc(draft['id'])}/missing-prices.xlsx">下载缺价清单</a>
  </div>
  <form method="post" action="/projects/{esc(project.id)}/drafts/{esc(draft['id'])}/bulk-supplement" enctype="multipart/form-data" data-loading="正在校验并合并补充表…" style="margin-top:12px">
    <input type="hidden" name="expected_revision" value="{esc(draft.get('revision') or 1)}">
    <div class="form-grid">
      <label class="field field--wide"><span>选择填好的 XLSX</span>
        <input class="file-input" type="file" name="upload" accept=".xlsx" required></label>
    </div>
    <div class="form-actions"><button class="btn btn--small" type="submit">上传并合并</button></div>
  </form>
  <p class="muted">补充表包含“设备”“管线”“安装项”三个工作表；没有的 sheet 留空。不会覆盖已有草稿行，只追加。</p>
</div>"""
    if data.get("scaleEditable", True):
        scale_input = f"""
  <label class="field" style="max-width:320px"><span>比例（米/图面单位，可人工确认）</span>
    <input id="draft-scale" form="draft-form" name="scale" value="{esc(data.get('scale') or '')}" placeholder="例如 0.001"></label>
  <p class="muted">修改比例并保存后，会按图面长度重算这张图的管线，比例相关的图纸疑点记为已确认；也可以直接修改下面的米数。</p>"""
    else:
        scale_input = (
            '<div class="form-grid">'
            + "".join(
                f'<label class="field"><span>{esc(item.get("fileName") or "本图纸")} 比例</span>'
                f'<input name="source_scale_{esc(item.get("inspectionId") or "")}" '
                f'value="{esc(source_scale_value(str(item.get("inspectionId") or "")))}" '
                'placeholder="例如 0.001"></label>'
                for item in multiplier_sources
            )
            + "</div>"
            '<p class="muted">多图纸合并时逐张确认比例；留空表示沿用检查比例。'
            "修改后保存，会按该图纸的图面长度重算管线，比例相关的图纸疑点记为已确认；"
            "也可以直接修改下表米数。</p>"
        )
    custom_items = list(data.get("customItems") or [])
    custom_rows = "".join(
        "<tr>"
        f"<td>{esc(item.get('floor'))}</td>"
        f"<td>{esc(item.get('name'))}</td>"
        f"<td>{esc(item.get('spec'))}</td>"
        f"<td>{esc(item.get('unit'))}</td>"
        f'<td class="num">{esc(item.get("quantity"))}</td>'
        f'<td class="num">{esc(item.get("material"))}</td>'
        f'<td class="num">{esc(item.get("installation"))}</td>'
        f'<td><button class="btn btn--small btn--ghost" type="submit" '
        f'name="custom_action" value="remove:{index}" formnovalidate>移除</button></td>'
        "</tr>"
        for index, item in enumerate(custom_items)
    )
    custom_table = (
        '<div class="table-wrap"><table class="table">'
        "<thead><tr><th>楼层</th><th>名称</th><th>规格</th><th>单位</th>"
        "<th>数量</th><th>材料价</th><th>安装价</th><th>操作</th></tr></thead>"
        f"<tbody>{custom_rows}</tbody></table></div>"
        if custom_rows
        else '<p class="muted">还没有补充项目。</p>'
    )
    second_books = [
        item for item in price_books if item.get("id") != price_book.get("id")
    ]
    selected_second = str(data.get("secondPriceBookId") or "")
    second_options = ['<option value="">不生成第二套</option>']
    for item in second_books:
        second_options.append(
            f'<option value="{esc(item.get("id"))}" '
            f'{"selected" if selected_second == item.get("id") else ""}>'
            f'{esc(item.get("name"))}</option>'
        )
    second_form = (
        f'<select name="secondPriceBookId">{"".join(second_options)}</select>'
        if second_books
        else (
            '<p class="muted">还没有第二份模板价格书。先到项目资料导入另一份 XLSX 模板，'
            "或到“新图纸报价”页导入后，再回来生成第二套辅材价。</p>"
        )
    )
    body = f"""
{_history_back("返回上一页", f"/projects/{project.id}/custom")}
<div class="page-head">
  <div>
    <p class="eyebrow">{esc(project.name)} · 新图纸草稿</p>
    <h1>{esc(data.get('title') or '新图纸报价')}</h1>
    <p class="lead">{esc(source_text)} · 价格书：{esc(draft.get('priceBookName') or '未选')}</p>
  </div>
</div>
<div class="steps"><span class="step done">1 图纸检查</span><span class="step done">2 价格导入</span><span class="step active">3 人工确认</span><span class="step">4 生成报价</span></div>
{_context_help(
    "怎么确认？",
    "逐组勾选要计入的设备和管线，选择模板行，核对数量/长度；缺型号/规格可就地填客户补价，确实缺失的部分可以在“还要补什么”里确认后不计入。",
    "/help#scope",
    "查看范围说明",
)}
{error_html}
{vision_html}
<form id="draft-form" method="post" action="/projects/{esc(project.id)}/drafts/{esc(draft['id'])}" data-loading="正在保存或生成报价…">
<input type="hidden" name="expected_revision" value="{esc(draft.get('revision') or 1)}">
<input type="hidden" name="publication_key" value="{esc(publication_key)}">
<div class="card">
  <h2>这一页怎么填</h2>
  <div class="guide-grid">
    <div><strong>设备组 / 管线组</strong><p>只处理你确认要计入的行：勾“计入”，核对数量或长度，选择模板行。模板没有的型号/规格，在该行填客户补价，不用另开表。</p></div>
    <div><strong>系数与税率</strong><p>系数默认 1，税率默认 0；生成前请按项目约定确认。</p></div>
    <div><strong>确实缺资料</strong><p>图纸疑点可在“还要补什么”里逐条核对；未识别的内容用“补录未识别内容”或批量补充表添加，不猜、不强行计价。</p></div>
  </div>
</div>
{recon_html}
{todo_html}
<div class="card" id="scale">
  <h2>图纸与比例</h2>
  <p class="muted">视图/楼层：{esc(_display_view(data.get('view')))}；图面比例：{esc(scale_summary)}。</p>
  <p class="muted">已识别：{esc(_draft_views_text(data))}</p>
  {f'<ul class="excluded">{source_html}</ul>' if source_html else ''}
  {scale_input}
</div>
<div class="card" id="drawing-issues">
  <h2>图纸疑点与资料缺口</h2>
  <p class="muted">这些是图纸本身读不全、单位不一致或未支持图元造成的疑点。逐条核对后勾“已核对，继续”，表示本次按现有识别结果报价，缺失部分用补录或客户补价处理；勾选会随报价留存。</p>
  <ul class="excluded">{drawing_issue_html}</ul>
  {drawing_review_html}
</div>
<div class="card">
  <h2>代表层数与采用范围</h2>
  <p class="muted">一张图代表多个相同楼层时，填代表层数 ×N；保存后设备数量和管线长度会按原识别量重算。会覆盖手工改过的数量。</p>
  <div class="form-grid">{multiplier_rows}</div>
  <div class="form-actions">
    <button class="btn btn--small btn--ghost" type="submit" name="action" value="multipliers" formnovalidate>重新应用代表层数</button>
  </div>
  <p class="muted">采用支路范围：在下面“管线组”逐组勾选正式采用的支路；未勾选的不进入报价。</p>
</div>
<div class="card" id="equipment">
  <h2>设备组</h2>
  <p class="muted">同一“套”价行包含室内外机时，把只作套内组成的一组勾为“套内组成”，再在成套组填写套数并勾“按套确认”，避免内外机重复计套。模板没有的型号，可就地填“客户补价”，不必先改模板价。</p>
  <div class="table-wrap"><table class="table draft-table">
    <colgroup><col style="width:52px"><col><col style="width:72px"><col style="width:250px"><col style="width:72px"><col style="width:76px"></colgroup>
    <thead><tr><th>计入</th><th>候选</th><th>数量</th><th>模板行</th><th>匹配</th><th>成套处理</th></tr></thead>
    <tbody>{''.join(equipment_rows) or '<tr><td colspan="6">没有设备候选</td></tr>'}</tbody>
  </table></div>
</div>
<div class="card" id="routes">
  <h2>采用支路范围（管线组）</h2>
  <p class="muted">只勾选本次正式采用的支路；未勾选的不进入报价。规格没读到的，可填“确认管径/规格”；模板没有对应行的，可填客户补价。</p>
  <div class="table-wrap"><table class="table draft-table">
    <colgroup><col style="width:52px"><col><col style="width:80px"><col style="width:250px"><col style="width:72px"><col style="width:180px"></colgroup>
    <thead><tr><th>计入</th><th>候选</th><th>长度（米）</th><th>模板行</th><th>匹配</th><th>联动保温</th></tr></thead>
    <tbody>{''.join(route_rows) or '<tr><td colspan="6">没有管线候选</td></tr>'}</tbody>
  </table></div>
  <p class="muted">铜管按液管/气管分规格计量；对应保温按已确认规则与管材等长，不需要在图上另画。水系统候选没有合适模板行时，可在候选下填写客户提供的人工材料价/安装价；人工价只进入本次报价的补充项目，不写回模板。</p>
</div>
<details class="card advanced-block" id="coefficients">
  <summary><h2>系数与税率</h2><span class="badge badge--info">请核对</span></summary>
  <div class="form-grid">
    <label class="field"><span>总体系数</span>
      <input name="globalCoefficient" value="{esc(data.get('globalCoefficient') or '1')}" placeholder="例如 1.2"></label>
    <label class="field"><span>设备系数（留空用总体）</span>
      <input name="equipmentCoefficient" value="{esc(data.get('equipmentCoefficient') or '')}" placeholder="留空则用总体系数"></label>
    <label class="field"><span>管线系数（留空用总体）</span>
      <input name="pipeCoefficient" value="{esc(data.get('pipeCoefficient') or '')}" placeholder="留空则用总体系数"></label>
    <label class="field"><span>税率/加计比例（例如 0.05）</span>
      <input name="taxRate" value="{esc(data.get('taxRate') or '0')}" placeholder="例如 0.06"></label>
    <label class="field"><span>计税基数</span>
      <select name="taxBasis">
        <option value="subtotal" {"selected" if data.get('taxBasis') in (None, '', 'subtotal') else ''}>整单未税金额</option>
        <option value="installation_and_auxiliary" {"selected" if data.get('taxBasis') == 'installation_and_auxiliary' else ''}>安装费与辅材，不含主设备材料</option>
      </select></label>
  </div>
  <p class="muted">系数只作用于汇总，不改明细单价；先算未税报价，再按所选计税基数加计。整单口径对全部未税金额计税；所选口径只对设备安装、辅材材料和辅材安装计税，主设备材料不进税基。加计比例与开票税率是两件事，开票条款请按合同确认。最终总额按两位四舍五入，Excel、PDF 与页面使用同一结果。安装费用采用已确认的价格；减免时请明确调整单价。</p>
</details>
<details class="card advanced-block">
  <summary><h2>第二套辅材价</h2><span class="badge badge--info">可选</span></summary>
  <p class="muted">选择另一份已导入的模板价格书，用其中的管线/保温等辅材价格生成第二套报价。设备仍用第一套模板价；第一套模板价方案保留，不会被覆盖。没有可靠新价来源时，不要用猜测的价格凑第二套。</p>
  <label class="field" style="max-width:420px"><span>第二套价格书</span>
    {second_form}
  </label>
</details>
<details class="card advanced-block" id="custom-items">
  <summary><h2>模板缺项补价</h2><span class="badge badge--info">可选</span></summary>
  <p class="muted">模板没有的型号或材料在这里补入本次报价：填写名称、规格、单位、数量、材料价和安装价。只写入本次工作副本，不修改母模板，也不会自动保存为模板新版本。</p>
  {custom_table}
  <div class="form-grid" style="margin-top:14px">
    <label class="field"><span>名称</span><input name="custom_name" placeholder="例如：MDV-450W 室外机"></label>
    <label class="field"><span>规格及型号</span><input name="custom_spec"></label>
    <label class="field"><span>分类</span>
      <select name="custom_category"><option value="equipment">设备</option><option value="pipe">管线/材料</option></select></label>
    <label class="field"><span>单位</span><input name="custom_unit" placeholder="台 / 米 / 项"></label>
    <label class="field"><span>楼层/区域</span><input name="custom_floor" value="补充项目"></label>
    <label class="field"><span>数量</span><input name="custom_quantity" type="number" min="0" step="0.001"></label>
    <label class="field"><span>材料单价</span><input name="custom_material" type="number" min="0" step="0.01" value="0"></label>
    <label class="field"><span>安装单价</span><input name="custom_installation" type="number" min="0" step="0.01" value="0"></label>
  </div>
  <div class="form-actions">
    <button class="btn btn--ghost" type="submit" name="custom_action" value="add" formnovalidate>加入这份报价</button>
  </div>
</details>
<details class="card advanced-block" id="manual-add">
  <summary><h2>补录未识别内容</h2><span class="badge badge--info">可选</span></summary>
  <p class="muted">图块/图层缺失或外部参照导致漏识别时，可手工补一组设备或管线；补录项同样需要选择模板行。模板没有该型号时，请使用上面的“模板缺项补价”，不要借用相近型号。</p>
  <div class="form-grid">
    <label class="field"><span>补录类型</span>
      <select name="add_kind">
        <option value="">不补录</option>
        <option value="equipment">设备</option>
        <option value="route">管线</option>
      </select></label>
    <label class="field"><span>楼层/视图</span>
      <input name="add_floor" value="人工补录"></label>
    <label class="field"><span>名称/型号</span>
      <input name="add_label" placeholder="例如：MDV-450W 室外机"></label>
    <label class="field"><span>模板行</span>
      <select name="add_price_row">{_price_options(price_book, None, "")}</select></label>
    <label class="field"><span>设备数量</span>
      <input name="add_quantity" type="number" min="0" step="1" placeholder="补设备时填写"></label>
    <label class="field"><span>管线类别</span>
      <input name="add_category" placeholder="补管线时填写，例如 冷媒管/水管/排水"></label>
    <label class="field"><span>管线长度（米）</span>
      <input name="add_length" type="number" min="0" step="any" placeholder="补管线时填写"></label>
  </div>
</details>
<div class="card">
  <h2>异常清单</h2>
  <ul class="excluded">{exception_html}</ul>
</div>
  <div class="form-actions">
    {('<label class="check-line" style="margin-right:14px"><input type="checkbox" name="vision_confirm" value="1"> 确认视觉差异并生成报价</label>' if (data.get("visionPreview") or {}).get("requiresConfirmation") else '')}
    <button class="btn btn--ghost" type="submit" name="action" value="save">保存草稿</button>
    <button class="btn btn--ghost" type="submit" name="action" value="add">添加补录</button>
    <button class="btn" type="submit" name="action" value="generate">生成报价</button>
    <a class="btn btn--ghost" href="/projects/{esc(project.id)}/custom">取消</a>
  </div>
</form>
{bulk_html}
"""
    return app_page(
        f"新图纸草稿 · {data.get('title') or project.name}",
        body,
        user,
        active="projects",
        project=project,
        crumbs=_crumb(
            (("我的项目", "/projects"), (project.name, f"/projects/{project.id}"), ("新图纸草稿", None))
        ),
    )






def quote_result_page(
    user: dict,
    project: Project,
    quote: QuoteRecord,
    payload: dict,
    *,
    labels: dict[int, dict] | None = None,
    excluded: tuple[str, ...] = (),
) -> str:
    labels = labels or {}
    equipment_rows, pipe_rows = _price_rows(payload, labels)
    totals = payload.get("totals", {})
    supplement = payload.get("supplement") or {}
    if not supplement:
        resolutions = payload.get("resolutions") or {}
        supplement = resolutions.get("supplement") or {}
    source = supplement.get("source") or {}
    needs_supplement = quote.status == "needs_supplement"
    status_badge = (
        '<span class="badge badge--warn">待补充</span>'
        if needs_supplement
        else (
            '<span class="badge badge--ok">已含客户补充</span>'
            if quote.supplemented
            else '<span class="badge badge--info">已生成</span>'
        )
    )
    supplement_note = ""
    if quote.supplemented:
        source_bits = [
            str(source.get("providedBy") or ""),
            str(source.get("date") or ""),
            str(source.get("note") or ""),
        ]
        source_text = " · ".join(item for item in source_bits if item)
        supplement_note = (
            f'<p class="muted">补充信息：{esc(source_text or "客户提供")}。'
            "补充数据未逐项回到图纸复核。</p>"
        )
    payload_omitted = [
        str(item.get("subject") or "")
        for item in payload.get("omitted") or []
        if str(item.get("subject") or "").strip()
    ]
    excluded_items = list(
        dict.fromkeys([*excluded, *payload_omitted])
    )
    excluded_html = "".join(
        f"<li>{esc(item)}</li>" for item in excluded_items
    )
    equipment_table = _group_table("设备与安装", equipment_rows)
    pipe_groups_html = "".join(
        _group_table(group["title"], group["rows"]) for group in pipe_rows
    )
    pipe_table = (
        '<div class="section-head"><h2>管线与辅材</h2>'
        '<span class="muted" style="font-size:12px">'
        "冷媒铜管 → 铜管保温 → 排水管 → 排水保温 → 其他辅材</span></div>"
        + pipe_groups_html
    )
    second = payload.get("secondScheme") or {}
    second_html = ""
    if second:
        second_html = f"""
<div class="card">
  <h2>第二套辅材价方案</h2>
  <p class="muted">只替换第二套价格书中的管线/保温等辅材价格，设备和安装仍按模板价；第一套模板价方案不变。</p>
  <div class="quote-summary" style="grid-template-columns:1fr 1fr;margin-top:10px">
    <section class="total-card" style="box-shadow:none">
      <small>第二套含税合计</small>
      <strong>{esc(money(second.get("total", "—")))}</strong>
      <div class="tax-line"><span>未税 <b>{esc(money(second.get("subtotal", "—")))}</b></span>
      <span>税额 <b>{esc(money(second.get("tax", "—")))}</b></span></div>
    </section>
    <section class="side-card" style="box-shadow:none">
      <p>价格书：{esc(second.get("priceBookName") or "—")}</p>
      <p class="muted">替换辅材行 {esc(len(second.get("overriddenRows") or []))} 行；两套报价使用同一工程量和范围。</p>
      <div class="download-row">
        <a class="btn btn--small" href="/projects/{esc(project.id)}/quotes/{esc(quote.run_id)}/download/quote-second.pdf">下载第二套 PDF</a>
        <a class="btn btn--small btn--ghost" href="/projects/{esc(project.id)}/quotes/{esc(quote.run_id)}/download/quote-second.xlsx">下载第二套 Excel</a>
      </div>
    </section>
  </div>
</div>"""
    supplement_action = ""
    if needs_supplement:
        supplement_action = f"""
<div class="notice">
  这份报价目前只包含图上已有的室内机。补充室外机成套或管线后，金额会更新，原记录仍可回看。
  <div class="download-row">
    <a class="btn btn--small" href="/projects/{esc(project.id)}/quotes/{esc(quote.run_id)}/supplement">补充室外机与管线</a>
  </div>
</div>"""
    body = f"""
{_history_back("返回上一步", f"/projects/{project.id}")}
<div class="quote-head">
  <div>
    <div>{status_badge}</div>
    <h1>{esc(quote.title)}</h1>
    <p class="meta">{esc(project.name)} · {esc(datetime_text(quote.updated_at))}</p>
  </div>
  <div class="page-actions">
    <a class="btn btn--ghost" href="/projects/{esc(project.id)}/new">再报一份</a>
  </div>
</div>
<div class="steps" aria-label="流程">
  <span class="step done">1 选择范围</span><span class="step done">2 核对图纸</span>
  <span class="step {'active' if needs_supplement else 'done'}">3 补充信息</span>
  <span class="step {'done' if not needs_supplement else ''}">4 生成报价</span>
  <span class="step {'active' if not needs_supplement else ''}">5 下载归档</span>
</div>
<div class="quote-summary">
  <section class="total-card">
    <small>含税合计</small>
    <strong>{esc(money(totals.get("total", "—")))}</strong>
    <div class="tax-line">
      <span>未税 <b>{esc(money(totals.get("subtotal", "—")))}</b></span>
      <span>税额 <b>{esc(money(totals.get("tax", "—")))}</b></span>
    </div>
  </section>
  <section class="side-card">
    <h3>这份报价</h3>
    <p>{esc(quote.title)}，按本次确认范围生成。</p>
    {supplement_note or '<p>当前报价不包含客户补充信息。</p>'}
    <div class="download-row">
      <a class="btn btn--small" href="/projects/{esc(project.id)}/quotes/{esc(quote.run_id)}/download/quote.pdf">下载 PDF</a>
      <a class="btn btn--small btn--ghost" href="/projects/{esc(project.id)}/quotes/{esc(quote.run_id)}/download/quote.xlsx">下载 Excel</a>
    </div>
    <p style="margin-top:12px"><a class="help-link" href="/help#result">看不懂金额和明细？查看说明 →</a></p>
  </section>
</div>
{supplement_action}
{second_html}
{equipment_table}
{pipe_table}
<div class="group-card">
  <h2>本报价未包含</h2>
  <ul class="excluded">{excluded_html}</ul>
</div>
"""
    return app_page(
        f"{quote.title} · 报价结果",
        body,
        user,
        active="projects",
        project=project,
        crumbs=_crumb((("我的项目", "/projects"), (project.name, f"/projects/{project.id}"), ("报价结果", None))),
    )


def _price_rows(payload: dict, labels: dict[int, dict]) -> tuple[list[dict], list[dict]]:
    items = {
        str(item.get("itemId")): item for item in payload.get("items", [])
    }
    equipment = presentation_equipment_rows(
        payload.get("priceLines", []), items, labels
    )
    pipes = build_groups(payload.get("priceLines", []), items, labels)
    for row in equipment:
        row["quantity"] = quantity(row.get("quantity"))
        row["amount"] = money(row.get("amount"))
    for group in pipes:
        for row in group["rows"]:
            row["quantity"] = quantity(row.get("quantity"))
            row["amount"] = money(row.get("amount"))
        group["total"] = money(group.get("total"))
    return equipment, pipes


def _group_table(title: str, rows: list[dict]) -> str:
    if not rows:
        return ""
    total = sum(Decimal(str(row["amount"]).replace(",", "")) for row in rows)
    body = "".join(
        f'<tr><td>{esc(row["name"])}</td>'
        f'<td>{esc(row.get("spec") or "—")}</td>'
        f'<td class="num">{esc(row["quantity"])} {esc(row["unit"])}</td>'
        f'<td class="num">{esc(row["amount"])}</td></tr>'
        for row in rows
    )
    return f"""
<section class="group-card">
  <div class="group-head"><h2>{esc(title)}</h2><span class="sum">{money(total)}</span></div>
  <div class="table-wrap">
    <table class="table">
      <thead><tr><th>项目</th><th>规格/型号</th><th>数量</th><th>合价</th></tr></thead>
      <tbody>{body}</tbody>
    </table>
  </div>
</section>"""


def delete_project_page(user: dict, project: Project) -> str:
    quote_count = len(project.quotes)
    body = f"""
<a class="back-link" href="/projects/{esc(project.id)}">← 返回项目</a>
<div class="form-card">
  <p class="eyebrow">删除项目</p>
  <h1>确认删除「{esc(project.name)}」？</h1>
  <p class="lead">这会删除项目记录和它的 {quote_count} 份报价文件，删除后无法恢复。</p>
  <div class="notice notice--danger">如果只是想重新报价，请返回项目内新建，不要删除项目。</div>
  <form id="delete-project-form" method="post" action="/projects/{esc(project.id)}/delete">
    <input type="hidden" name="confirm" value="yes">
    <label style="display:flex;gap:9px;align-items:flex-start;font-size:13px;color:#4b4b50">
      <input id="delete-confirm" type="checkbox" style="margin-top:4px">
      <span>我确认删除该项目及其全部报价文件。</span>
    </label>
    <div class="form-actions">
      <button id="delete-submit" class="btn btn--danger" type="submit" disabled>删除项目</button>
      <a class="btn btn--ghost" href="/projects/{esc(project.id)}">取消</a>
    </div>
  </form>
</div>
"""
    return app_page(
        "删除项目 · HVAC Quote 工程报价",
        body,
        user,
        active="projects",
        project=project,
        crumbs=_crumb((("我的项目", "/projects"), (project.name, f"/projects/{project.id}"), ("删除项目", None))),
    )


def help_page(user: dict | None = None) -> str:
    cards = []
    for index, article in enumerate(HELP_ARTICLES, start=1):
        keywords = " ".join(
            (article.title, article.summary, *article.keywords)
        )
        steps = "".join(f"<li>{esc(step)}</li>" for step in article.steps)
        problems = "".join(
            f'<div class="help-problem"><strong>{esc(problem.question)}</strong>'
            f"<p>{esc(problem.answer)}</p></div>"
            for problem in article.problems
        )
        cards.append(
            f'<details class="help-card" id="{esc(article.key)}" '
            f'data-help-card data-keywords="{esc(keywords)}">'
            "<summary>"
            f'<span class="help-card-icon">{index:02d}</span>'
            "<div>"
            f'<div class="help-card-title">{esc(article.title)}</div>'
            f'<p class="help-card-summary">{esc(article.summary)}</p>'
            '<span class="help-card-open">展开步骤</span>'
            "</div></summary>"
            '<div class="help-card-body">'
            f"<h3>怎么做</h3><ol>{steps}</ol>"
            f'<h3>常见问题</h3><div class="help-problems">{problems}</div>'
            "</div></details>"
        )
    if user:
        back_html = (
            '<a class="back-link" href="/projects" '
            'onclick="if(document.referrer && document.referrer.indexOf(location.origin)===0)'
            '{history.back();return false;}">← 返回上一页</a>'
            '<a class="text-link" href="/projects">项目总览</a>'
        )
    else:
        back_html = '<a class="back-link" href="/">← 返回首页</a>'
    body = f"""
<div class="help-back-row">{back_html}</div>
<div class="page-head">
  <div>
    <p class="eyebrow">使用帮助</p>
    <h1>帮助中心</h1>
    <p class="lead">选一个正在做的操作，先看六步流程；需要细节时再展开卡片。</p>
  </div>
</div>
<div class="help-toolbar">
  <input id="help-filter" class="help-search" placeholder="想了解什么？例如：上传、补充、下载" aria-label="筛选帮助主题">
  <span id="help-count" class="help-count">{len(HELP_ARTICLES)} / {len(HELP_ARTICLES)} 篇</span>
</div>
{_workflow_flow_html()}
<div class="help-grid">{''.join(cards)}</div>
<div id="help-empty" class="help-empty" hidden>没有匹配的帮助主题。换个关键词试试，例如“上传”“补充”“下载”。</div>
<div class="notice notice--info" style="margin-top:18px">
  <strong>还是没解决？</strong> 当前系统以客户图纸和模板的检查结果为准；新增图纸、楼层或模板需要人工确认范围和计价，不能只靠上传文件自动生成。
</div>
"""
    if user:
        return app_page(
            "帮助中心 · HVAC Quote 工程报价",
            body,
            user,
            active="help",
            crumbs=_crumb((("使用帮助", None),)),
        )
    return public_page("帮助中心 · HVAC Quote 工程报价", body, None)




def error_page(
    message: str,
    *,
    user: dict | None = None,
    title: str = "这次没有完成",
    back_href: str = "/projects",
    back_label: str = "返回项目总览",
) -> str:
    body = f"""
<div class="card empty" style="margin-top:30px">
  <h2>{esc(title)}</h2>
  <p>{esc(message)}</p>
  <div class="download-row" style="justify-content:center">
    <a class="btn" href="{esc(back_href)}">{esc(back_label)}</a>
    <a class="btn btn--ghost" href="/help">查看使用帮助</a>
  </div>
</div>
"""
    if user:
        return app_page("操作未完成 · HVAC Quote 工程报价", body, user)
    return public_page("操作未完成 · HVAC Quote 工程报价", body, None)


def internal_page(title: str, body: str) -> str:
    content = f"""
<div class="internal-wrap">
  <div class="internal-card">
    <p class="muted" style="margin:0">HVAC Quote · 内部链路</p>
    <h1 style="margin:6px 0 4px">{esc(title)}</h1>
    <p class="muted" style="margin:0">内部示例页，不面向客户。</p>
  </div>
  {body}
</div>"""
    return document(title, content, "internal-body")


def vision_preview_page(
    user: dict,
    project: Project,
    inspection: dict,
    draft: dict,
    preview: dict,
    config: dict,
) -> str:
    image = preview.get("image") or {}
    candidate_rows = "".join(
        "<tr>"
        f"<td>{esc(item.get('label') or item.get('sourceKey'))}</td>"
        f"<td>{'设备' if item.get('kind') == 'equipment' else '管线'}</td>"
        f"<td>{esc(item.get('layer') or '')}</td>"
        f"<td>{esc(item.get('spec') or '')}</td>"
        f"<td class='num'>{esc(item.get('quantity') or '')}</td>"
        f"<td class='num'>{esc(item.get('lengthM') if item.get('lengthM') is not None else '待核')}</td>"
        "</tr>"
        for item in preview.get("candidates") or []
    )
    body = f"""
{_history_back("返回检查页", f"/projects/{project.id}/inspections/{inspection['id']}")}
<div class="page-head">
  <div>
    <p class="eyebrow">{esc(project.name)} · 视觉辅助</p>
    <h1>确认将发送的局部</h1>
    <p class="lead">这是服务器按你选中的候选生成的局部线框图；只有点“确认发送”才会调用模型。</p>
  </div>
</div>
<div class="notice notice--info">
  模型：{esc(config.get("model") or "未配置")} ·
  状态：{'可用' if config.get("available") else '未配置或连接失败'} ·
  发送内容：1 张局部 PNG（{esc(image.get("width") or "?")}×{esc(image.get("height") or "?")}），
  不含整张图纸。
</div>
<div class="card">
  <h2>局部预览</h2>
  <p><img src="/projects/{esc(project.id)}/vision-previews/{esc(preview['previewId'])}/crop.png"
    alt="选中的局部裁图" style="max-width:100%;border:1px solid #dde4ea;border-radius:12px;background:#fff"></p>
  <p class="muted">裁图身份：{esc(image.get("cropId") or "")[:20]}… ·
  SHA256：{esc(image.get("pngSha256") or "")[:20]}… ·
  范围：{esc(", ".join(str(round(float(v), 1)) for v in (preview.get("manifest") or {}).get("cadBBox") or []))}</p>
</div>
<div class="card">
  <h2>选中的候选</h2>
  <div class="table-wrap"><table class="table">
    <thead><tr><th>名称</th><th>类型</th><th>图层</th><th>规格</th><th>数量</th><th>米制长度</th></tr></thead>
    <tbody>{candidate_rows}</tbody>
  </table></div>
</div>
<div class="card">
  <h2>确认发送</h2>
  <p class="muted">目标草稿：{esc((draft.get("data") or {}).get("title") or draft.get("drawingName") or draft.get("id"))} ·
  检查版本：{esc(preview.get("inspectionVersion") or "")[:28]}…</p>
  <form method="post" action="/projects/{esc(project.id)}/inspections/{esc(inspection['id'])}/vision-tasks" data-loading="正在提交视觉任务…">
    <input type="hidden" name="preview_id" value="{esc(preview['previewId'])}">
    <input type="hidden" name="draft_id" value="{esc(draft['id'])}">
    <input type="hidden" name="idempotency_key" value="vtask-{esc(preview['previewId'])}">
    <input type="hidden" name="opt_in" value="1">
    <label class="check-line"><input type="checkbox" required> 我确认只发送上面这张局部图，并理解模型建议必须逐条人工确认</label>
    <div class="form-actions">
      <button class="btn" type="submit">确认发送并生成建议</button>
      <a class="btn btn--ghost" href="/projects/{esc(project.id)}/inspections/{esc(inspection['id'])}">取消</a>
    </div>
  </form>
  <p class="muted">同一预览重复点击只对应一个任务；不会重复调用模型。取消或关闭开关不会撤销已确认报价。</p>
</div>
"""
    return app_page(
        "视觉辅助预览 · HVAC Quote 工程报价",
        body,
        user,
        active="projects",
        project=project,
        crumbs=_crumb(
            (
                ("我的项目", "/projects"),
                (project.name, f"/projects/{project.id}"),
                ("视觉辅助预览", None),
            )
        ),
    )


def _vision_candidate_label(candidates: dict, candidate_id: str) -> str:
    item = candidates.get(candidate_id) or {}
    return str(item.get("label") or item.get("sourceKey") or candidate_id)


def vision_task_page(
    user: dict,
    project: Project,
    draft: dict | None,
    task: dict,
    *,
    config: dict | None = None,
) -> str:
    status = str(task.get("status") or "")
    status_labels = {
        "queued": "排队中",
        "running": "模型处理中",
        "suggestions_ready": "建议已生成",
        "failed": "失败",
        "timeout": "超时",
        "cancelled": "已取消",
        "interrupted": "服务重启中断",
    }
    status_label = status_labels.get(status, status)
    meta_refresh = (
        '<meta http-equiv="refresh" content="3">'
        if status in {"queued", "running"}
        else ""
    )
    candidates = {
        str(item.get("candidateId")): item
        for item in task.get("candidates") or []
    }
    draft_revision = int((draft or {}).get("revision") or 0)
    image_html = ""
    if task.get("images"):
        image_html = (
            f'<p><img src="/projects/{esc(project.id)}/vision-tasks/{esc(task["taskId"])}/crop.png" '
            'alt="局部裁图" style="max-width:100%;border:1px solid #dde4ea;border-radius:12px;background:#fff"></p>'
        )
    control_html = ""
    if status in {"queued", "running"}:
        control_html = (
            f'<form method="post" action="/projects/{esc(project.id)}/vision-tasks/{esc(task["taskId"])}/cancel">'
            '<button class="btn btn--small btn--ghost" type="submit">取消任务</button></form>'
            "<p class=\"muted\">已发出的网络请求无法撤回；迟到结果不会自动采纳。取消未完成任务不会修改报价。</p>"
        )
    elif status in {"failed", "timeout", "cancelled", "interrupted"}:
        control_html = (
            f'<form method="post" action="/projects/{esc(project.id)}/vision-tasks/{esc(task["taskId"])}/retry">'
            '<button class="btn btn--small" type="submit">由我主动重试</button></form>'
            "<p class=\"muted\">失败不会改动原检查或草稿；也可以返回普通报价流程。</p>"
        )
    suggestions_html = ""
    if status == "suggestions_ready":
        blocks = []
        for item in (task.get("suggestion") or {}).get("decisions") or []:
            item_id = str(item.get("suggestionItemId") or "")
            candidate_labels = "、".join(
                _vision_candidate_label(candidates, value)
                for value in item.get("candidateIds") or []
            )
            first_candidate = candidates.get(
                (item.get("candidateIds") or [""])[0], {}
            )
            kind = first_candidate.get("kind")
            model_value = item.get("modelSuggestion") or {}
            model_line = (
                f'<p class="muted">型号建议：{esc(model_value.get("model"))}'
                f'（原生标注：{esc(model_value.get("annotationId") or "无")}；'
                f'疑点：{esc(model_value.get("concern") or "无")}）</p>'
                if model_value
                else '<p class="muted">型号建议：无（未从 reason 抽取型号）。</p>'
            )
            category_options = "".join(
                f'<option value="{value}" {"selected" if value == item.get("category") else ""}>{value}</option>'
                for value in sorted(ALLOWED_CATEGORIES)
            )
            medium_options = "".join(
                f'<option value="{value}" {"selected" if value == item.get("medium") else ""}>{value}</option>'
                for value in sorted(ALLOWED_MEDIA)
            )
            role_options = "".join(
                f'<option value="{value}" {"selected" if value == item.get("role") else ""}>{value}</option>'
                for value in sorted(ALLOWED_ROLES)
            )
            extra_inputs = ""
            if kind == "equipment":
                extra_inputs = (
                    '<label class="field"><span>人工型号（可选）</span>'
                    f'<input name="model__{esc(item_id)}" maxlength="200" '
                    f'value="{esc(model_value.get("model") or "")}" placeholder="仅在有原生文字依据时填写"></label>'
                )
            else:
                extra_inputs = (
                    '<label class="field"><span>人工规格（可选）</span>'
                    f'<input name="spec__{esc(item_id)}" maxlength="80" '
                    f'value="{esc(first_candidate.get("spec") or "")}" placeholder="例如 DN40 / φ15.88"></label>'
                )
            blocks.append(
                f"""
<div class="card">
  <h2>{esc(candidate_labels)}</h2>
  <p class="muted">原判断：{esc(first_candidate.get("category") or "")} /
  {esc(first_candidate.get("medium") or "")} / {esc(first_candidate.get("role") or "")}；
  模型：{esc(item.get("category"))} / {esc(item.get("medium"))} / {esc(item.get("role"))}；
  置信度 {esc(item.get("uncertainty"))}。</p>
  <p class="muted">依据：{esc(item.get("reason") or "")}</p>
  {model_line}
  <div class="form-grid">
    <label class="field"><span>决定</span>
      <select name="decision__{esc(item_id)}">
        <option value="pending" selected>待核（不改报价）</option>
        <option value="accept">接受/修改后采用</option>
        <option value="reject">拒绝（不改报价）</option>
      </select></label>
    <label class="field"><span>类别</span><select name="category__{esc(item_id)}">{category_options}</select></label>
    <label class="field"><span>介质</span><select name="medium__{esc(item_id)}">{medium_options}</select></label>
    <label class="field"><span>角色</span><select name="role__{esc(item_id)}">{role_options}</select></label>
    {extra_inputs}
  </div>
  <p class="muted">选择“接受”后，服务器按原生 CAD 长度/数量重算，再按客户模板套价；不会采用模型给出的量价。</p>
</div>"""
            )
        unresolved = "".join(
            f"<li>{esc('、'.join(_vision_candidate_label(candidates, value) for value in item.get('candidateIds') or []))}：{esc(item.get('reason') or '')}</li>"
            for item in (task.get("suggestion") or {}).get("unresolved") or []
        )
        suggestions_html = (
            f'<div class="notice notice--success">建议已生成，请逐条选择；选择“待核/拒绝”不会改动草稿。</div>'
            f'{"<div class=\"card\"><h2>未解决</h2><ul class=\"excluded\">" + unresolved + "</ul></div>" if unresolved else ""}'
            f'<form method="post" action="/projects/{esc(project.id)}/vision-tasks/{esc(task["taskId"])}/decision">'
            f'<input type="hidden" name="draft_id" value="{esc((draft or {}).get("id") or "")}">'
            f'<input type="hidden" name="task_version" value="{esc(task.get("taskVersion"))}">'
            f'<input type="hidden" name="suggestion_version" value="{esc(task.get("suggestionVersion"))}">'
            f'<input type="hidden" name="expected_revision" value="{esc(draft_revision)}">'
            + "".join(blocks)
            + '<div class="form-actions"><button class="btn" type="submit">确认选择并重算差异</button>'
            f'<a class="btn btn--ghost" href="/projects/{esc(project.id)}/drafts/{esc((draft or {}).get("id") or "")}">返回草稿</a></div></form>'
        )
    error_html = (
        f'<div class="alert" role="alert">原因：{esc(task.get("errorMessage") or task.get("errorCode") or "未知")}</div>'
        if status in {"failed", "timeout"}
        else ""
    )
    body = f"""
{meta_refresh}
{_history_back("返回草稿", f"/projects/{esc(project.id)}/drafts/{esc((draft or {}).get('id') or '')}")}
<div class="page-head">
  <div>
    <p class="eyebrow">{esc(project.name)} · 视觉辅助</p>
    <h1>视觉任务 · {esc(status_label)}</h1>
    <p class="lead">模型：{esc(task.get("model") or "未配置")} ·
    发送次数：{esc(task.get("attempts") or 0)} · 耗时：{esc(task.get("durationMs") or 0)} ms</p>
  </div>
</div>
{error_html}
<div class="card">
  <h2>任务状态</h2>
  <p>当前状态：<strong>{esc(status_label)}</strong>；任务版本 {esc(task.get("taskVersion") or 0)}；
  建议版本 {esc(str(task.get("suggestionVersion") or "")[:16])}。</p>
  {image_html}
  {control_html}
</div>
{suggestions_html}
"""
    return app_page(
        f"视觉辅助 · {project.name}",
        body,
        user,
        active="projects",
        project=project,
        crumbs=_crumb(
            (
                ("我的项目", "/projects"),
                (project.name, f"/projects/{project.id}"),
                ("视觉辅助", None),
            )
        ),
    )
