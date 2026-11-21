import { api } from '../api.js';

export async function renderDead(view, setMsg) {
  const [vouchers, nodes] = await Promise.all([
    api('/api/dead-letters'),
    api('/api/nodes'),
  ]);
  const items = vouchers.items || [];
  const catalog = (nodes.items || []).map((n) => n.node_code).join('、');
  view.innerHTML = `
    <section class="card">
      <h2>死信页 · 断边旁路账册</h2>
      <p class="muted">
        命运模式：<b>${vouchers.mode}</b>（独立队列吞没模式不实现、不可并存）；
        当前隔离条数（待核齐凭证）：<b id="isolated">${vouchers.isolated_count}</b>
      </p>
      <p class="muted">目录对照（节点目录在册编码）：${catalog || '空'}</p>
      <div class="row">
        <label>边号<input id="s-code" value="E-SCAN-1" /></label>
        <label>上游编码<input id="s-up" value="N-GHOST-TBL" /></label>
        <label>下游编码<input id="s-down" value="N-DWS-REV" /></label>
        <label>变换<input id="s-kind" value="直抽" /></label>
        <button class="primary" id="scan" type="button">扫描结果去重入库</button>
      </div>
      <p class="muted">服务端一次落库：健康边进主表，断边写死信凭证；指纹对不上现行边会被拦下并返回中文原因。</p>
      <pre id="scan-report" class="muted"></pre>
    </section>
    <section class="wall" style="margin-top:1rem">
      ${items.map((v) => `<article class="card dead-card">
        <h3>${v.voucher_code} · ${v.edge_code}</h3>
        <span class="tag ${v.status === '待核齐' ? 'tag-dead' : 'tag-ok'}">${v.status}</span>
        <span class="tag">${v.source}</span>
        ${v.edge_status ? `<span class="tag ${v.edge_status === '已死信' ? 'tag-dead' : ''}">主表：${v.edge_status}</span>` : '<span class="tag">主表无此边</span>'}
        <p>${v.upstream_ref} → ${v.downstream_ref}</p>
        <p class="muted">${v.transform_kind}</p>
        <p class="muted">指纹 <code>${v.short_fingerprint}…</code></p>
        <p class="muted">来由：${v.reason || '—'} · 经手 ${v.created_by} · ${v.created_at}</p>
        ${v.status === '待核齐' ? `<button data-rec="${v.id}" class="primary" type="button">核齐</button>`
          : `<p class="muted">已于 ${v.reconciled_at || ''} 核齐${v.note ? '：' + v.note : ''}</p>`}
      </article>`).join('') || '<p class="muted">暂无死信凭证</p>'}
    </section>`;

  document.getElementById('scan').onclick = async () => {
    try {
      const payload = {
        items: [{
          edge_code: document.getElementById('s-code').value.trim(),
          upstream_code: document.getElementById('s-up').value.trim(),
          downstream_code: document.getElementById('s-down').value.trim(),
          transform_kind: document.getElementById('s-kind').value.trim(),
        }],
      };
      const r = await api('/api/dead-letters/scan-ingest', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      const rep = r.report;
      document.getElementById('scan-report').textContent =
        `入库 ${rep.ingested_count} · 死信 ${rep.dead_count} · 拦截 ${rep.blocked_count}` +
        (rep.blocked.length ? '\n拦截原因：' + rep.blocked.map((b) => `${b.edge_code}：${b.reason}`).join('\n') : '') +
        (rep.dead.length ? '\n死信：' + rep.dead.map((d) => `${d.edge_code}（${d.voucher}）`).join('；') : '');
      setMsg(rep.blocked_count ? '有断边被拦住，请看报告' : '扫描入库完成');
      await renderDead(view, setMsg);
    } catch (e) { setMsg(e.message, true); }
  };

  view.querySelectorAll('[data-rec]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        const r = await api('/api/dead-letters/' + btn.dataset.rec + '/reconcile', {
          method: 'POST',
          body: JSON.stringify({ note: '人工核齐' }),
        });
        setMsg('凭证 ' + r.voucher.voucher_code + ' 已核齐');
        await renderDead(view, setMsg);
      } catch (e) { setMsg(e.message, true); }
    };
  });
}
