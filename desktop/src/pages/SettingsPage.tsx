import type { FormEvent } from "react";
import type {
  AppSettings,
  BackupSnapshot,
  MigrationReport,
  OnboardingStatus,
  ProviderCheck,
  RecruitmentApi,
  TaskAction,
  TaskStatus,
  VendorPreset,
} from "../App";
import type { AiCatalog, AiConfig, AiConnectionUpdate, AiStatus } from "../ai/types";
import { AiServicesPanel } from "../ai/AiServicesPanel";
import { AdvancedAiSettings } from "../ai/AdvancedAiSettings";
import { ConnectionWizard } from "../ai/ConnectionWizard";
import { RemindersPanel } from "../cases/RemindersPanel";
import { IndexSyncPanel } from "../search/IndexSyncPanel";
import { Button } from "../components/ui";

const HEALTH_LABELS: Record<string, string> = {
  database: "数据库",
  blob_store: "原件库",
  search: "检索引擎",
  disk: "磁盘空间",
};

const PROVIDER_LABELS: Record<string, string> = {
  llm: "大模型",
  embedding: "向量模型",
  reranker: "重排模型",
  web_search: "网页搜索",
};

function taskLabel(task: TaskStatus): string {
  const isBackfill = task.task_type.startsWith("BACKFILL_");
  const verb = isBackfill ? "回填" : "解析";
  if (task.status === "SUCCESS") return `${verb}完成`;
  if (task.status === "FAILED" || task.status === "DEAD_LETTER") return `${verb}失败`;
  if (task.status === "PAUSED") return "已暂停";
  if (task.status === "CANCELLED") return "已取消";
  return `${verb}中 ${task.progress}%`;
}

export interface SettingsPageProps {
  api: RecruitmentApi;
  onboarding: OnboardingStatus | null;
  settings: AppSettings;
  vendors: VendorPreset[];
  providerChecks: ProviderCheck[];
  aiApiMessage: string;
  aiCatalog: AiCatalog | null;
  aiConfig: AiConfig | null;
  aiStatus: AiStatus | null;
  aiBusy: boolean;
  aiMessage: string;
  aiWizard: null | "primary" | "secondary";
  aiAdvanced: boolean;
  mailMessage: string;
  mailTestResult: { imap: { ok: boolean; message: string }; smtp: { ok: boolean; message: string } } | null;
  mailSyncMessage: string;
  mailStatus: { configured: boolean; last_uid: number } | null;
  mailWhitelistInput: string;
  dataRootInput: string;
  dataRootMessage: string;
  health: Record<string, { status: string; message?: string }> | null;
  backups: BackupSnapshot[];
  portableBackupPath: string;
  portableRestorePath: string;
  portableRestoreTarget: string;
  portablePassphrase: string;
  portableMessage: string;
  backupBusy: boolean;
  portableBusy: boolean;
  migrationTarget: string;
  migrationReport: MigrationReport | null;
  migrationBusy: boolean;
  migrationMessage: string;
  backfillMessage: string;
  tasks: TaskStatus[];
  onSettingsChange: (settings: AppSettings) => void;
  onMailWhitelistInputChange: (value: string) => void;
  onDataRootInputChange: (value: string) => void;
  onPortableBackupPathChange: (value: string) => void;
  onPortablePassphraseChange: (value: string) => void;
  onPortableRestorePathChange: (value: string) => void;
  onPortableRestoreTargetChange: (value: string) => void;
  onMigrationTargetChange: (value: string) => void;
  onLoadSettings: () => void;
  onLoadOnboarding: () => void;
  onRestartApp: () => void;
  onSaveAiApiSettings: (event: FormEvent) => void;
  onTestProviders: () => void;
  onAddAiService: (slot: "primary" | "secondary") => void;
  onCloseAiWizard: () => void;
  onToggleAiAdvanced: () => void;
  onSaveAiConnection: (connection: AiConnectionUpdate) => Promise<string | null>;
  onUpdateAiConnections: (connections: AiConnectionUpdate[]) => Promise<string | null>;
  onRefreshAiCatalog: () => void;
  onSaveMailSettings: (event: FormEvent) => void;
  onTestMailConfig: () => void;
  onSyncMailNow: () => void;
  onLoadMailStatus: () => void;
  onSendFollowupTest: () => void;
  onAddMailWhitelistTag: () => void;
  onRemoveMailWhitelistTag: (tag: string) => void;
  onSaveDataRoot: () => void;
  onCheckHealth: () => void;
  onExportDiagnostics: () => void;
  onLoadBackups: () => void;
  onCreateBackup: () => void;
  onRestoreBackupItem: (filename: string) => void;
  onCreatePortableBackup: () => void;
  onRestorePortableBackup: () => void;
  onRunBackfill: (kind: "school-mappings" | "candidate-profiles" | "jd-profiles" | "reparse-failed") => void;
  onControlTask: (task: TaskStatus, action: TaskAction) => void;
  onMigrateData: (event: FormEvent) => void;
  onOpenCase: (caseId: string) => void;
}

export function SettingsPage(props: SettingsPageProps) {
  const {
    api, onboarding, settings, providerChecks, aiApiMessage,
    aiCatalog, aiConfig, aiStatus, aiBusy, aiMessage, aiWizard, aiAdvanced,
    mailMessage,
    mailTestResult, mailSyncMessage, mailStatus, mailWhitelistInput, dataRootInput,
    dataRootMessage, health, backups, portableBackupPath, portableRestorePath,
    portableRestoreTarget, portablePassphrase, portableMessage, backupBusy, portableBusy,
    migrationTarget, migrationReport, migrationBusy, migrationMessage, backfillMessage, tasks,
    onSettingsChange, onMailWhitelistInputChange, onDataRootInputChange,
    onPortableBackupPathChange, onPortablePassphraseChange, onPortableRestorePathChange,
    onPortableRestoreTargetChange, onMigrationTargetChange, onLoadSettings, onLoadOnboarding,
    onRestartApp, onSaveAiApiSettings, onTestProviders, onAddAiService, onCloseAiWizard,
    onToggleAiAdvanced, onSaveAiConnection, onUpdateAiConnections, onRefreshAiCatalog,
    onSaveMailSettings, onTestMailConfig,
    onSyncMailNow, onLoadMailStatus, onSendFollowupTest, onAddMailWhitelistTag,
    onRemoveMailWhitelistTag, onSaveDataRoot, onCheckHealth, onExportDiagnostics,
    onLoadBackups, onCreateBackup, onRestoreBackupItem, onCreatePortableBackup,
    onRestorePortableBackup, onRunBackfill, onControlTask, onMigrateData, onOpenCase,
  } = props;

  return (
    <section className="jd-panel">
      <div className="section">
        <div className="section-head">
          <h2>启动检查</h2>
          <div className="actions">
            <Button variant="secondary" onClick={onLoadSettings}>重新加载设置</Button>
            <Button variant="secondary" onClick={onLoadOnboarding}>运行检查</Button>
            <Button variant="secondary" onClick={onRestartApp}>重启应用</Button>
          </div>
        </div>
        {onboarding && (
          <div className="metric-grid">
            <div className="metric"><small>数据目录</small><strong>{onboarding.data_root}</strong></div>
            <div className="metric"><small>LLM</small><strong className={onboarding.llm_enabled ? "ok" : "bad"}>{onboarding.llm_enabled ? "已配置" : "未配置"}</strong></div>
            <div className="metric"><small>向量/重排</small><strong className={onboarding.search_enabled ? "ok" : "bad"}>{onboarding.search_enabled ? "已配置" : "本地"}</strong></div>
            <div className="metric"><small>BD 搜索</small><strong className={onboarding.bd_search_enabled ? "ok" : "bad"}>{onboarding.bd_search_enabled ? "已配置" : "未配置"}</strong></div>
            {Object.entries(onboarding.health).map(([name, component]) => (
              <div key={name} className="metric">
                <small>{HEALTH_LABELS[name] ?? name}</small>
                <strong className={component.status === "healthy" ? "ok" : "bad"}>{component.status === "healthy" ? "正常" : "异常"}</strong>
              </div>
            ))}
          </div>
        )}
      </div>

      <AiServicesPanel
        catalog={aiCatalog}
        config={aiConfig}
        status={aiStatus}
        busy={aiBusy}
        message={aiMessage}
        onAdd={onAddAiService}
        onOpenAdvanced={onToggleAiAdvanced}
      />

      {aiAdvanced && <AdvancedAiSettings api={api} catalog={aiCatalog} config={aiConfig} busy={aiBusy} onSave={onUpdateAiConnections} onRefreshCatalog={onRefreshAiCatalog} />}

      {aiWizard && (
        <ConnectionWizard
          api={api}
          catalog={aiCatalog}
          slot={aiWizard}
          onClose={onCloseAiWizard}
          onSave={onSaveAiConnection}
        />
      )}

      <form className="section" onSubmit={(event) => void onSaveAiApiSettings(event)}>
        <div className="section-head">
          <h2>检索服务（高级）</h2>
          <div className="actions">
            <Button type="button" variant="secondary" onClick={onTestProviders}>测试检索服务</Button>
            <Button type="submit" variant="primary">保存检索服务配置</Button>
          </div>
        </div>
        <p className="muted">配置向量（Embedding）、重排（Rerank）与网页搜索服务；与上面的 AI 服务相互独立，保存后需重启应用生效。</p>
        <div className="form-grid">
          <input value={settings.siliconflow_api_key ?? ""} onChange={(e) => onSettingsChange({ ...settings, siliconflow_api_key: e.target.value })} placeholder="SiliconFlow API Key（向量/重排）" aria-label="SiliconFlow API Key" />
          <input value={settings.tavily_api_key ?? ""} onChange={(e) => onSettingsChange({ ...settings, tavily_api_key: e.target.value })} placeholder="Tavily API Key（网页搜索）" aria-label="Tavily API Key" />
        </div>
        {aiApiMessage && <p className="muted">{aiApiMessage}</p>}
        {providerChecks.length > 0 && (
          <div className="metric-grid">
            {providerChecks.map((check) => (
              <div className="metric" key={check.name}>
                <small>{PROVIDER_LABELS[check.name] ?? check.name}</small>
                <strong className={check.ok ? "ok" : "bad"}>{check.ok ? "正常" : "异常"}：{check.message}</strong>
              </div>
            ))}
          </div>
        )}
      </form>

      <form className="section" onSubmit={(event) => void onSaveMailSettings(event)}>
        <div className="section-head">
          <h2>邮箱与提醒</h2>
          <div className="actions">
            <Button type="submit" variant="primary">保存邮箱配置</Button>
          </div>
        </div>
        <p className="muted">配置 IMAP 收件（简历邮件入库）与 SMTP 发件（提醒通知）的服务器、账号和授权码；保存后系统会发送一封绑定确认邮件，且需重启应用生效。</p>
        <div className="form-grid">
          <select
            aria-label="邮箱预设"
            value=""
            onChange={(e) => {
              const host = e.target.value;
              if (host) onSettingsChange({ ...settings, imap_host: host, smtp_host: host.replace("imap.", "smtp.") });
            }}
          >
            <option value="">邮箱预设</option>
            <option value="imap.qq.com">QQ 邮箱</option>
            <option value="imap.163.com">163 邮箱</option>
          </select>
          <label className="switch" style={{ padding: 0, background: "none", border: 0, alignItems: "center" }}>
            <input type="checkbox" checked={settings.smtp_ssl !== false} onChange={(e) => onSettingsChange({ ...settings, smtp_ssl: e.target.checked })} />
            <span className="slider" />
            <span style={{ fontSize: 13 }}>SMTP SSL 加密</span>
          </label>
        </div>
        <div className="form-grid">
          <input value={settings.imap_host ?? ""} onChange={(e) => onSettingsChange({ ...settings, imap_host: e.target.value })} placeholder="IMAP 主机" aria-label="IMAP 主机" />
          <input value={settings.imap_account ?? ""} onChange={(e) => onSettingsChange({ ...settings, imap_account: e.target.value })} placeholder="IMAP 账号" aria-label="IMAP 账号" />
        </div>
        <div className="form-grid">
          <input value={settings.imap_auth_code ?? ""} onChange={(e) => onSettingsChange({ ...settings, imap_auth_code: e.target.value })} placeholder="IMAP 授权码" aria-label="IMAP 授权码" />
          <input value={settings.smtp_host ?? ""} onChange={(e) => onSettingsChange({ ...settings, smtp_host: e.target.value })} placeholder="SMTP 主机" aria-label="SMTP 主机" />
        </div>
        <div className="form-grid">
          <input value={settings.smtp_port ?? ""} onChange={(e) => onSettingsChange({ ...settings, smtp_port: e.target.value ? Number(e.target.value) : undefined })} placeholder="SMTP 端口" aria-label="SMTP 端口" type="number" />
          <input value={settings.smtp_account ?? ""} onChange={(e) => onSettingsChange({ ...settings, smtp_account: e.target.value })} placeholder="SMTP 账号" aria-label="SMTP 账号" />
        </div>
        <div className="form-grid">
          <input value={settings.smtp_auth_code ?? ""} onChange={(e) => onSettingsChange({ ...settings, smtp_auth_code: e.target.value })} placeholder="SMTP 授权码" aria-label="SMTP 授权码" />
          <input value={settings.reminder_to ?? ""} onChange={(e) => onSettingsChange({ ...settings, reminder_to: e.target.value })} placeholder="提醒收件人邮箱" aria-label="提醒收件人邮箱" />
        </div>
        <label className={"switch" + (settings.mail_auto_sync === true ? " on" : "")} style={{ marginTop: 14 }}>
          <input type="checkbox" checked={settings.mail_auto_sync === true} onChange={(e) => onSettingsChange({ ...settings, mail_auto_sync: e.target.checked })} />
          <span className="slider" />
          <span className="txt">
            <strong>自动拉取简历</strong>
            <small>每 5 分钟自动从收件箱拉取简历附件；关闭后仍可手动「立即同步」</small>
          </span>
        </label>
        {settings.imap_host && settings.mail_auto_sync !== true && (
          <p className="muted" style={{ color: "#b26a00" }}>已配置邮箱但自动拉取已关闭；可开启「自动拉取简历」或使用「立即同步」手动拉取。</p>
        )}
        <label className={"switch" + (settings.daily_followup_enabled === true ? " on" : "")} style={{ marginTop: 12 }}>
          <input type="checkbox" checked={settings.daily_followup_enabled === true} onChange={(e) => onSettingsChange({ ...settings, daily_followup_enabled: e.target.checked })} />
          <span className="slider" />
          <span className="txt">
            <strong>每日待跟进报告</strong>
            <small>整理推荐未反馈 / 明日面试 / 面试未反馈，每日 21:30 与次日 09:00 各发送一次</small>
          </span>
        </label>
        <p className="muted">QQ/163 邮箱需在邮箱设置中开启 IMAP/SMTP，并使用「授权码」作为密码，而非登录密码。</p>
        <div className="sub-label">发件人白名单</div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
          {(settings.imap_whitelist ?? "").split(",").map((s) => s.trim()).filter(Boolean).map((tag) => (
            <span key={tag} className="tag">
              {tag}
              <button type="button" onClick={() => onRemoveMailWhitelistTag(tag)}>×</button>
            </span>
          ))}
        </div>
        <div className="form-grid" style={{ marginTop: 10 }}>
          <input value={mailWhitelistInput} onChange={(e) => onMailWhitelistInputChange(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); onAddMailWhitelistTag(); } }} placeholder="添加发件人白名单（域名或完整邮箱）" aria-label="添加发件人白名单" />
          <Button type="button" variant="secondary" onClick={onAddMailWhitelistTag} style={{ justifySelf: "start" }}>添加</Button>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 14 }}>
          <Button type="button" variant="secondary" onClick={onTestMailConfig}>连接测试</Button>
          <Button type="button" variant="secondary" onClick={onSyncMailNow}>立即同步</Button>
          <Button type="button" variant="secondary" onClick={onLoadMailStatus}>刷新同步状态</Button>
          <Button type="button" variant="secondary" onClick={onSendFollowupTest}>发送测试报告</Button>
        </div>
        {mailTestResult && (
          <div className="metric-grid">
            <div className="metric"><small>IMAP</small><strong>{mailTestResult.imap.ok ? "正常" : "异常"}：{mailTestResult.imap.message}</strong></div>
            <div className="metric"><small>SMTP</small><strong>{mailTestResult.smtp.ok ? "正常" : "异常"}：{mailTestResult.smtp.message}</strong></div>
          </div>
        )}
        {mailSyncMessage && <p className="muted">{mailSyncMessage}</p>}
        {mailStatus && (
          <p className="muted">邮箱已{mailStatus.configured ? "配置" : "未配置"} · 已同步收件 UID：{mailStatus.last_uid}</p>
        )}
        {mailMessage && <p className="muted">{mailMessage}</p>}
      </form>

      <RemindersPanel api={api} onOpenCase={onOpenCase} />

      <div className="section">
        <div className="section-head"><h2>数据目录</h2></div>
        <p className="muted">存放数据库、简历原件、检索引擎等全部本地数据的根目录；输入绝对路径点「设置数据目录」后重启应用生效（留空使用默认目录）。</p>
        <div className="form-grid" style={{ marginTop: 10 }}>
          <input value={dataRootInput} onChange={(e) => onDataRootInputChange(e.target.value)} placeholder="数据目录绝对路径（留空使用默认）" aria-label="数据目录" />
          <Button type="button" variant="secondary" onClick={onSaveDataRoot} style={{ justifySelf: "start" }}>设置数据目录</Button>
        </div>
        {dataRootMessage && <p className="muted">{dataRootMessage}</p>}
      </div>

      <IndexSyncPanel api={api} />

      <div className="section">
        <div className="section-head">
          <h2>健康检测台</h2>
          <div className="actions">
            <Button variant="secondary" onClick={onCheckHealth}>运行检测</Button>
            <Button variant="secondary" onClick={onExportDiagnostics}>导出诊断信息</Button>
          </div>
        </div>
        <p className="muted">检测数据库、原件库、检索引擎、磁盘四项健康状态；点「运行检测」查看各项状态，点「导出诊断信息」导出完整诊断 JSON。</p>
        {health && (
          <div className="metric-grid">
            {Object.entries(health).map(([name, component]) => (
              <div key={name} className="metric">
                <small>{HEALTH_LABELS[name] ?? name}</small>
                <strong className={component.status === "healthy" ? "ok" : "bad"}>{component.status === "healthy" ? "正常" : "异常"}</strong>
                {component.message && <p>{component.message}</p>}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="section">
        <div className="section-head">
          <h2>备份与恢复</h2>
          <div className="actions">
            <Button variant="secondary" disabled={backupBusy} onClick={onLoadBackups}>加载备份</Button>
            <Button variant="secondary" disabled={backupBusy} onClick={onCreateBackup}>{backupBusy ? "备份中…" : "立即备份"}</Button>
          </div>
        </div>
        <p className="muted">创建本地数据库快照备份，或用加密口令生成便携备份迁移到其他设备；点「立即备份」/「恢复」操作快照，输入路径与口令创建/恢复便携备份。</p>
        {backups.length === 0 ? (
          <p className="muted">暂无备份快照</p>
        ) : (
          <div className="row-list">
            {backups.map((b) => (
              <div key={b.filename} className="row-item">
                <div>
                  <strong>{b.filename}</strong>
                  <small>{b.created}</small>
                </div>
                <Button variant="ghost" size="sm" disabled={backupBusy} onClick={() => onRestoreBackupItem(b.filename)}>恢复</Button>
              </div>
            ))}
          </div>
        )}
        <div className="form-grid" style={{ marginTop: 16 }}>
          <input
            value={portableBackupPath}
            onChange={(event) => onPortableBackupPathChange(event.target.value)}
            placeholder="便携备份文件路径"
            aria-label="便携备份路径"
          />
          <input
            value={portablePassphrase}
            onChange={(event) => onPortablePassphraseChange(event.target.value)}
            placeholder="加密口令"
            aria-label="便携备份口令"
            type="password"
          />
        </div>
        <Button type="button" variant="secondary" disabled={portableBusy} onClick={onCreatePortableBackup} style={{ marginTop: 10 }}>{portableBusy ? "创建中…" : "创建加密便携备份"}</Button>
        <div className="form-grid" style={{ marginTop: 16 }}>
          <input
            value={portableRestorePath}
            onChange={(event) => onPortableRestorePathChange(event.target.value)}
            placeholder="待恢复的便携备份文件"
            aria-label="便携备份恢复文件"
          />
          <input
            value={portableRestoreTarget}
            onChange={(event) => onPortableRestoreTargetChange(event.target.value)}
            placeholder="恢复目标目录"
            aria-label="便携备份恢复目录"
          />
        </div>
        <Button type="button" variant="secondary" disabled={portableBusy} onClick={onRestorePortableBackup} style={{ marginTop: 10 }}>{portableBusy ? "恢复中…" : "恢复便携备份"}</Button>
        {portableMessage && <p role="status">{portableMessage}</p>}
      </div>

      <div className="section">
        <div className="section-head"><h2>数据回填</h2></div>
        <p className="muted">为存量候选人/JD 补齐学校标签与 AI 画像。回填按实体幂等执行，不会覆盖人工修改；可在下方查看进度，任务运行中支持暂停、恢复与重试。</p>
        <div className="actions" style={{ marginTop: 8, flexWrap: "wrap" }}>
          <Button type="button" variant="secondary" onClick={() => onRunBackfill("school-mappings")}>学校映射回填</Button>
          <Button type="button" variant="secondary" onClick={() => onRunBackfill("candidate-profiles")}>候选人画像回填</Button>
          <Button type="button" variant="secondary" onClick={() => onRunBackfill("jd-profiles")}>JD 画像回填</Button>
        </div>
        {backfillMessage && <p role="status" className="muted">{backfillMessage}</p>}
        {tasks.filter((task) => task.task_type.startsWith("BACKFILL_")).map((task) => (
          <div key={task.id} className="row-item">
            <div>
              <strong>{taskLabel(task)}</strong>
              <small>{task.task_type} · {task.status}</small>
            </div>
            <div className="actions">
              <progress max={100} value={task.progress} style={{ width: 140 }} />
              {(task.status === "RUNNING" || task.status === "QUEUED" || task.status === "PENDING" || task.status === "RETRY_WAIT") && (
                <Button type="button" variant="ghost" size="sm" onClick={() => onControlTask(task, "pause")}>暂停</Button>
              )}
              {task.status === "PAUSED" && (
                <Button type="button" variant="ghost" size="sm" onClick={() => onControlTask(task, "resume")}>恢复</Button>
              )}
              {(task.status === "DEAD_LETTER" || task.status === "FAILED") && (
                <Button type="button" variant="ghost" size="sm" onClick={() => onControlTask(task, "retry")}>重试</Button>
              )}
              {!["SUCCESS", "CANCELLED", "DEAD_LETTER"].includes(task.status) && (
                <Button type="button" variant="ghost" size="sm" onClick={() => onControlTask(task, "cancel")}>取消</Button>
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="section">
        <div className="section-head"><h2>数据迁移</h2></div>
        <p className="muted">把当前数据复制到新目录并校验完整性；输入新目录绝对路径点「复制并校验」，通过后重启应用切换数据目录。</p>
        <form className="form-grid" style={{ marginTop: 10 }} onSubmit={(event) => void onMigrateData(event)}>
          <input value={migrationTarget} onChange={(e) => onMigrationTargetChange(e.target.value)} placeholder="新数据目录（绝对路径）" aria-label="迁移目标目录" />
          <Button type="submit" variant="primary" disabled={migrationBusy} style={{ justifySelf: "start" }}>{migrationBusy ? "迁移中…" : "复制并校验"}</Button>
        </form>
        {migrationReport && (
          <div className="row-list">
            <div className="row-item">
              <div>
                <strong>{migrationReport.ok ? "迁移校验通过" : "迁移校验未通过"}</strong>
                <small>{migrationReport.target_root} · {migrationReport.files_verified}/{migrationReport.files_copied} 文件 · {migrationReport.candidate_count} 候选人</small>
              </div>
            </div>
          </div>
        )}
        {migrationMessage && <p role="status" className="muted">{migrationMessage}</p>}
      </div>
    </section>
  );
}
