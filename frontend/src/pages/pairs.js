import { chip, empty, formError, formFooter, heading, icon } from '../components/common/ui.js';
import { ORDER } from '../constants/enums.js';
import { store } from '../stores/workspace.js';
import { SOURCE, SHEET_SOURCE, completePair, includedPair } from '../types/records.js';
import { esc } from '../utils/format.js';

const outcome = value => value === null ? chip('缺测', 'amber') : value ? chip('成功', 'green') : chip('未成功');

function pairState(row) {
  if (row.excluded) return chip('暂不纳入', 'amber');
  return completePair(row) ? chip('纳入统计', 'green') : chip('缺失 · 未入组', 'amber');
}

function importPanel() {
  const { imports } = store.study;
  const batches = imports?.batches || [];
  const segments = imports?.segments || [];
  const conflicts = segments.filter(seg => seg.conflict);
  return `<section class="sheet import-panel"><div class="section-title"><h2>交付表导入</h2>${chip(`${batches.length} 个批次`)}</div>
    <p class="muted">旧展签表与易读样张飞表可分开上传、隔天才到也行。同一段取最高更正号；最高更正号有不同结果时整对暂不纳入统计，待更高更正号补齐后自动恢复。文件名相同的重传不会重复计人。</p>
    <div class="import-actions">
      <button class="primary" data-edit="import-old">${icon('plus')} 导入旧展签表</button>
      <button class="primary" data-edit="import-new">${icon('plus')} 导入易读样张飞表</button>
    </div>
    ${conflicts.length ? `<p class="warning">${icon('export')} 有 ${conflicts.length} 个观众段存在最高更正号冲突，相关配对已暂不纳入。</p>` : ''}
    ${batches.length ? `<div class="table-scroll"><table class="batch-table"><thead><tr><th>批次</th><th>来源文件</th><th>行数</th><th>状态</th><th>导入人</th></tr></thead><tbody>
      ${batches.map(b => `<tr><td>${chip(SHEET_SOURCE[b.source] || b.source)}</td><td class="mono">${esc(b.filename)}</td><td>${b.row_count}</td><td>${b.status === 'reconciled' ? '已对账' : '已接收'}</td><td>${esc(b.imported_by)}</td></tr>`).join('')}
    </tbody></table></div>` : '<p class="muted">还没有导入记录，可继续使用手工登记。</p>'}
  </section>`;
}

export function pairsPage() {
  const { study, user, pairFilter } = store;
  const editable = user.role === 'analyst';
  const actions = study && editable
    ? `<button class="primary" data-edit="pair">${icon('plus')} 登记配对</button><button data-edit="import-old">${icon('export')} 导入交付表</button>`
    : '';
  const head = heading('观众测读', '配对记录', '每个匿名观众编号对应两次阅读，保留先后顺序、缺测一侧与导入对账状态。', actions);
  if (!study) return head + empty('暂无批次', '请在版本对照页创建复验批次。');
  const counts = study.summary;
  const filters = {
    all: ['全部', counts.registered], included: ['纳入统计', counts.total],
    complete: ['完整配对', counts.complete], excluded: ['暂不纳入', counts.excluded],
  };
  const visible = study.pairs.filter(row => {
    if (pairFilter === 'all') return true;
    if (pairFilter === 'included') return includedPair(row);
    if (pairFilter === 'excluded') return !includedPair(row);
    return completePair(row) === (pairFilter === 'complete');
  });
  return `${head}<section class="study-banner"><div><p class="mono">${esc(study.code)} / 测读任务</p><p>${esc(study.reading_task)}</p></div>${chip(`样本修订 R${study.revision}`)}</section>
    ${editable ? importPanel() : ''}
    <section class="sheet table-sheet"><div class="table-tools"><div class="tabs" aria-label="筛选配对状态">${Object.entries(filters).map(([key, [name, count]]) => `<button data-filter="${key}" aria-pressed="${pairFilter === key}">${name} <b>${count}</b></button>`).join('')}</div><input id="pair-search" aria-label="搜索观众编号" placeholder="搜索观众编号"></div>
    <div class="table-scroll pair-scroll"><table><thead><tr><th>观众编号</th><th>来源</th><th>阅读顺序</th><th>旧版结果</th><th>样张结果</th><th>入组情况</th>${editable ? '<th class="right">操作</th>' : ''}</tr></thead><tbody>${visible.map(row => `<tr data-visitor="${esc(row.visitor_code)}"><td class="mono">${esc(row.visitor_code)}</td><td>${chip(SOURCE[row.source] || '手录')}</td><td>${ORDER[row.reading_order]}</td><td>${outcome(row.old_ok)}</td><td>${outcome(row.new_ok)}</td><td>${pairState(row)}${row.excluded && row.exclude_reason ? `<br><small class="muted">${esc(row.exclude_reason)}</small>` : ''}</td>${editable ? `<td class="right">${row.source === 'import' ? '<span class="muted">以导入为准</span>' : `<button class="text-button" data-edit="pair" data-code="${esc(row.visitor_code)}">编辑</button>`}</td>` : ''}</tr>`).join('')}</tbody></table><p class="empty row-empty" id="pair-empty" ${visible.length ? 'hidden' : ''}>没有符合条件的配对记录。</p></div></section>`;
}

export function pairForm(code) {
  const row = store.study.pairs.find(pair => pair.visitor_code === code);
  const select = (name, caption) => `<label>${caption}<select name="${name}"><option value="" ${row?.[name] == null ? 'selected' : ''}>缺测</option><option value="true" ${row?.[name] === true ? 'selected' : ''}>成功</option><option value="false" ${row?.[name] === false ? 'selected' : ''}>未成功</option></select></label>`;
  return `<form data-form="pair"><h2>${row ? '编辑配对' : '登记配对'}</h2><p class="muted">未观察到的一侧请保留为「缺测」。导入来源的配对请用重新导入更正。</p>
    <div class="field-grid"><label>观众编号<input name="visitor_code" required maxlength="30" pattern="[A-Z0-9][A-Z0-9\\-]{1,29}" value="${esc(code)}" ${row ? 'readonly' : ''} placeholder="PD-37"></label><label>阅读顺序<select name="reading_order">${Object.entries(ORDER).map(([key, text]) => `<option value="${key}" ${row?.reading_order === key ? 'selected' : ''}>${text}</option>`).join('')}</select></label></div>
    <div class="field-grid">${select('old_ok', '旧版结果')}${select('new_ok', '样张结果')}</div>
    ${formError}${formFooter('保存配对')}</form>`;
}

export function importForm(source) {
  const label = SHEET_SOURCE[source];
  return `<form data-form="import" data-source="${source}"><h2>导入${label}</h2>
    <p class="muted">上传 UTF-8 编码 CSV，表头须含 visitor_code、correction_no、reading_order、outcome；outcome 留空或写「缺测」表示未观测。文件名作为批次幂等键。</p>
    <label>来源文件<input type="file" name="file" accept=".csv,text/csv" required></label>
    <label>或粘贴 JSON 行（可选，优先使用上传文件）<textarea name="json_rows" rows="4" placeholder='[{"visitor_code":"PD-01","correction_no":0,"reading_order":"old_first","outcome":true}]'></textarea></label>
    ${formError}${formFooter('导入并对账')}</form>`;
}
