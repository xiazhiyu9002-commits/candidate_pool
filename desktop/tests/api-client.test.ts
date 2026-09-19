import { describe, expect, test, vi } from "vitest";
import { invoke } from "@tauri-apps/api/core";
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(async () => null) }));

import { ApiClient } from "../src/api/client";


describe("ApiClient", () => {
  test("opens Word by authoritative metadata through the default-app command", async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ kind: "word", filename: "简历.DOCX", path: "C:/managed/open-documents/a.docx" })));
    const client = new ApiClient("http://localhost", "token", fetcher);
    expect(await client.viewResume("r")).toEqual({ kind: "opened", filename: "简历.DOCX" });
    expect(invoke).toHaveBeenCalledWith("open_document", { path: "C:/managed/open-documents/a.docx" });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  test("preserves specific preview errors from the backend", async () => {
    const client = new ApiClient("http://localhost", "token", async () => new Response(JSON.stringify({ message: "原始文件不存在" }), { status: 404 }));
    await expect(client.previewResume("missing")).rejects.toThrow("原始文件不存在");
  });
  test("preserves reminder case linkage, paused state and explicit Shanghai timestamps", async () => {
    const requests: Request[] = [];
    const linked = { id: "reminder-1", title: "跟进张三", note: null, remind_at: "2026-09-02T01:30:00Z", dismissed: false, dismissed_at: null, case_id: "case-1", paused_by_workflow: true };
    const client = new ApiClient("http://localhost", "token", async (input, init) => {
      const request = new Request(input, init);
      requests.push(request);
      return new Response(JSON.stringify(request.method === "GET" ? [linked] : linked), { status: 200 });
    });
    const created = await client.createReminder({ title: "跟进张三", remind_at: "2026-09-02T09:30:00+08:00", case_id: "case-1" });
    expect(await requests[0].json()).toEqual({ title: "跟进张三", remind_at: "2026-09-02T09:30:00+08:00", case_id: "case-1" });
    expect(created).toEqual(linked);
    expect(await client.listReminders()).toEqual([linked]);
    await client.createReminder({ title: "独立提醒", remind_at: "2026-09-02T09:30:00+08:00" });
    expect(await requests[2].json()).toEqual({ title: "独立提醒", remind_at: "2026-09-02T09:30:00+08:00" });
  });

  test("requests only index retry work and reads resume review at the HTTP boundary", async () => {
    const requests: Request[] = [];
    const client = new ApiClient("http://localhost", "token", async (input, init) => {
      requests.push(new Request(input, init));
      return new Response("{}", { status: 200 });
    });
    await client.indexStatus();
    await client.retryIndexSync();
    await client.getResumeReview("revision/1");
    expect(requests.map((request) => [request.method, new URL(request.url).pathname])).toEqual([
      ["GET", "/api/search/index-status"], ["POST", "/api/search/index-retry"],
      ["GET", "/api/resumes/revisions/revision%2F1/review"],
    ]);
    expect(await requests[1].json()).toEqual({});
  });

  test("preserves action time, note and retry identity across every workflow endpoint", async () => {
    const requests: Request[] = [];
    const client = new ApiClient("http://localhost", "token", async (input, init) => {
      requests.push(new Request(input, init));
      return new Response("{}", { status: 200 });
    });
    const payload = { occurred_at: "2026-08-30T15:30:00+08:00", note: "确认", idempotency_key: "stable-retry" };
    await client.recommendCase("case-1", payload);
    await client.enterInterview("case-1", { ...payload, round_name: "加面" });
    await client.recordResult("case-1", "round-1", "通过", payload);
    await client.passAndAdvance("case-1", "round-1", { ...payload, next_round_name: "HR" });
    await client.offerCase("case-1", payload);
    await client.onboardCase("case-1", payload);
    await client.exitCase("case-1", "候选人退出", payload);
    await client.voidEvent("event-1", payload);
    expect(await Promise.all(requests.map((request) => request.json()))).toEqual([
      payload, { ...payload, round_name: "加面" }, { ...payload, case_round_id: "round-1", result: "通过" },
      { ...payload, case_round_id: "round-1", next_round_name: "HR" }, payload, payload,
      { ...payload, result: "候选人退出" }, payload,
    ]);
  });

  test("adds the per-launch session token to JSON requests", async () => {
    let received: Request | undefined;
    const fetcher: typeof fetch = async (input, init) => {
      received = new Request(input, init);
      return new Response(JSON.stringify({ items: [], degraded_reasons: [] }), {
        headers: { "Content-Type": "application/json" },
        status: 200
      });
    };
    const client = new ApiClient("http://127.0.0.1:43127", "launch-token", fetcher);

    await client.searchCandidates("Python 金融");

    expect(received?.url).toBe("http://127.0.0.1:43127/api/search/candidates");
    expect(received?.headers.get("X-Kerui-Session")).toBe("launch-token");
    expect(await received?.json()).toEqual({ query: "Python 金融", mode: "hybrid", operator: "smart", rewrite_enabled: false, search_body: false, limit: 100, offset: 0 });
  });

  test("serializes keyword operator and disabled rewrite as snake_case", async () => {
    let received: Request | undefined;
    const fetcher: typeof fetch = async (input, init) => {
      received = new Request(input, init);
      return new Response(JSON.stringify({ items: [], degraded_reasons: [] }), { status: 200 });
    };
    const client = new ApiClient("http://127.0.0.1:43127", "token", fetcher);

    await client.searchCandidates("Java 后端", undefined, { mode: "keyword", operator: "and", rewriteEnabled: false });

    expect(await received?.json()).toEqual({ query: "Java 后端", mode: "keyword", operator: "and", rewrite_enabled: false, search_body: false, limit: 2000, offset: 0 });
  });

  test("serializes hybrid rewrite enabled", async () => {
    let received: Request | undefined;
    const fetcher: typeof fetch = async (input, init) => {
      received = new Request(input, init);
      return new Response(JSON.stringify({ items: [], degraded_reasons: [] }), { status: 200 });
    };
    const client = new ApiClient("http://127.0.0.1:43127", "token", fetcher);

    await client.searchCandidates("交易系统后端", undefined, { mode: "hybrid", operator: "smart", rewriteEnabled: true });

    expect(await received?.json()).toEqual({ query: "交易系统后端", mode: "hybrid", operator: "smart", rewrite_enabled: true, search_body: false, limit: 100, offset: 0 });
  });

  test("forwards explicit search filters without overriding natural-language defaults", async () => {
    const requests: Request[] = [];
    const client = new ApiClient("http://localhost", "token", async (input, init) => {
      requests.push(new Request(input, init));
      return new Response(JSON.stringify(requests.length === 1
        ? { items: [], degraded_reasons: [] }
        : { items: [], total: 123, page: 2, page_size: 50, has_more: true }), { status: 200 });
    });
    await client.searchCandidates("Python 上海", {
      min_years: 5, highest_degree: "BACHELOR", locations: ["上海", "苏州"],
      preferred_locations: ["北京"], exclude_skills: ["外包"],
    });
    await client.listCandidatesPage(2, 50);
    expect(await requests[0].json()).toEqual({
      query: "Python 上海", mode: "hybrid", operator: "smart", rewrite_enabled: false, search_body: false, limit: 100, offset: 0,
      filters: { min_years: 5, highest_degree: "BACHELOR", locations: ["上海", "苏州"],
        preferred_locations: ["北京"], exclude_skills: ["外包"] },
    });
    expect(requests[1].url).toBe("http://localhost/api/resumes/candidates/page?page=2&page_size=50");
  });

  test("falls back to the legacy candidate list when an older sidecar lacks paging", async () => {
    const urls: string[] = [];
    const legacyItems = ["one", "two", "three"].map((name, index) => ({
      candidate_id: `candidate-${index + 1}`,
      revision_id: `revision-${index + 1}`,
      display_name: name,
      total_years: null,
      highest_degree: null,
      location: null,
      status: "AVAILABLE",
      revision_status: "READY",
      phone: null,
      original_filename: `${name}.pdf`,
      parsed_data: null,
    }));
    const client = new ApiClient("http://localhost", "token", async (input) => {
      const url = String(input);
      urls.push(url);
      if (url.includes("/candidates/page")) {
        return new Response(JSON.stringify({ detail: "Not Found" }), { status: 404 });
      }
      return new Response(JSON.stringify(legacyItems), { status: 200 });
    });

    const page = await client.listCandidatesPage(2, 2);

    expect(urls).toEqual([
      "http://localhost/api/resumes/candidates/page?page=2&page_size=2",
      "http://localhost/api/resumes/candidates",
    ]);
    expect(page.items.map((item) => item.display_name)).toEqual(["three"]);
    expect(page).toMatchObject({ total: 3, page: 2, page_size: 2, has_more: false });
  });

  test("lets the browser set the multipart upload boundary", async () => {
    let received: Request | undefined;
    let suppliedHeaders: Headers | undefined;
    const fetcher: typeof fetch = async (input, init) => {
      suppliedHeaders = new Headers(init?.headers);
      received = new Request(input, init);
      return new Response(JSON.stringify({
        candidate_id: "candidate-1",
        document_id: "document-1",
        revision_id: "revision-1",
        blob_id: "blob-1",
        task_id: "task-1"
      }), { headers: { "Content-Type": "application/json" }, status: 200 });
    };
    const client = new ApiClient("http://127.0.0.1:43127", "launch-token", fetcher);

    await client.importResume(new File(["resume"], "resume.pdf", { type: "application/pdf" }));

    expect(received?.body).not.toBeNull();
    expect(suppliedHeaders?.has("Content-Type")).toBe(false);
  });

  test("surfaces the stable Chinese API error message", async () => {
    const fetcher: typeof fetch = async () => new Response(JSON.stringify({
      code: "E_FILE_TYPE_UNSUPPORTED",
      message: "仅支持 PDF、DOC 和 DOCX 简历",
      request_id: "request-1",
      details: null
    }), { headers: { "Content-Type": "application/json" }, status: 400 });
    const client = new ApiClient("http://127.0.0.1:43127", "launch-token", fetcher);

    await expect(client.importResume(new File(["x"], "resume.txt"))).rejects.toThrow(
      "仅支持 PDF、DOC 和 DOCX 简历"
    );
  });

  test("diagnostics requests the correct path", async () => {
    let received: Request | undefined;
    const fetcher: typeof fetch = async (input, init) => {
      received = new Request(input, init);
      return new Response(JSON.stringify({
        sqlite_version: "3.45.3",
        database_path: "/tmp/db",
        database_size_bytes: 1024,
        counts: {},
        pragmas: {}
      }), { headers: { "Content-Type": "application/json" }, status: 200 });
    };
    const client = new ApiClient("http://127.0.0.1:43127", "launch-token", fetcher);

    await client.diagnostics();

    expect(received?.url).toBe("http://127.0.0.1:43127/api/diagnostics");
  });

  test("buildMappingTree posts text and label to the project", async () => {
    let received: Request | undefined;
    const fetcher: typeof fetch = async (input, init) => {
      received = new Request(input, init);
      return new Response(JSON.stringify({ id: "snap-1", label: "v1", is_current: true }), {
        headers: { "Content-Type": "application/json" },
        status: 200
      });
    };
    const client = new ApiClient("http://127.0.0.1:43127", "launch-token", fetcher);

    await client.buildMappingTree("proj-1", "字节跳动\n  技术部", "v1");

    expect(received?.url).toBe("http://127.0.0.1:43127/api/mapping/projects/proj-1/build-from-text");
    expect(await received?.json()).toEqual({ text: "字节跳动\n  技术部", label: "v1" });
  });

  test("searchBdLeads posts query and limit", async () => {
    let received: Request | undefined;
    const fetcher: typeof fetch = async (input, init) => {
      received = new Request(input, init);
      return new Response(JSON.stringify([]), {
        headers: { "Content-Type": "application/json" },
        status: 200
      });
    };
    const client = new ApiClient("http://127.0.0.1:43127", "launch-token", fetcher);

    await client.searchBdLeads("Java 工程师", 20);

    expect(received?.url).toBe("http://127.0.0.1:43127/api/bd/search");
    expect(await received?.json()).toEqual({ query: "Java 工程师", limit: 20 });
  });

  test("provider connectivity uses the onboarding probe endpoint", async () => {
    let received: Request | undefined;
    const fetcher: typeof fetch = async (input, init) => {
      received = new Request(input, init);
      return new Response(JSON.stringify([{ name: "llm", ok: true, message: "可用" }]), {
        headers: { "Content-Type": "application/json" },
        status: 200
      });
    };
    const client = new ApiClient("http://127.0.0.1:43127", "launch-token", fetcher);

    await client.testProviders();

    expect(received?.url).toBe("http://127.0.0.1:43127/api/onboarding/test-providers");
    expect(received?.method).toBe("POST");
  });

  test("lists tasks and posts task control actions", async () => {
    const received: Request[] = [];
    const fetcher: typeof fetch = async (input, init) => {
      received.push(new Request(input, init));
      return new Response(JSON.stringify([]), {
        headers: { "Content-Type": "application/json" },
        status: 200
      });
    };
    const client = new ApiClient("http://127.0.0.1:43127", "launch-token", fetcher);

    await client.listTasks();
    await client.controlTask("task/1", "pause");

    expect(received[0].url).toBe("http://127.0.0.1:43127/api/tasks");
    expect(received[0].method).toBe("GET");
    expect(received[1].url).toBe("http://127.0.0.1:43127/api/tasks/task%2F1/pause");
    expect(received[1].method).toBe("POST");
  });

  test("imports JD files and switches resume revisions", async () => {
    const received: Request[] = [];
    const fetcher: typeof fetch = async (input, init) => {
      received.push(new Request(input, init));
      const payload = received.length === 1
        ? { jd_id: "jd-1", revision_id: "jd-rev-1" }
        : { revision_id: "resume-rev-1", original_filename: "resume.pdf", status: "READY", is_current: true, created_at: "2026-08-30T00:00:00Z" };
      return new Response(JSON.stringify(payload), {
        headers: { "Content-Type": "application/json" },
        status: 200
      });
    };
    const client = new ApiClient("http://127.0.0.1:43127", "launch-token", fetcher);

    await client.importJdFile(new File(["jd"], "岗位.docx"), "某公司", "后端工程师");
    await client.switchResumeRevision("resume/rev-1");

    expect(received[0].url).toBe("http://127.0.0.1:43127/api/jd/import-file");
    expect(received[0].method).toBe("POST");
    expect(received[0].headers.get("Content-Type")).toMatch(/^multipart\/form-data; boundary=/);
    expect(received[1].url).toBe("http://127.0.0.1:43127/api/resumes/revisions/resume%2Frev-1/switch");
    expect(received[1].method).toBe("POST");
  });

  test("posts batch matches and result marks", async () => {
    const received: Request[] = [];
    const fetcher: typeof fetch = async (input, init) => {
      received.push(new Request(input, init));
      return new Response(JSON.stringify(received.length === 1
        ? { results: [] }
        : { result_id: "result-1", status: "保留" }), {
        headers: { "Content-Type": "application/json" },
        status: 200
      });
    };
    const client = new ApiClient("http://127.0.0.1:43127", "launch-token", fetcher);

    await client.matchBatch(["rev-1", "rev-2"], 30);
    await client.markMatchResult("result/1", "保留");

    expect(received[0].url).toBe("http://127.0.0.1:43127/api/match/batch");
    expect(await received[0].json()).toEqual({ revision_ids: ["rev-1", "rev-2"], limit: 30, mode: "hybrid" });
    expect(received[1].url).toBe("http://127.0.0.1:43127/api/match/result/result%2F1/mark");
    expect(await received[1].json()).toEqual({ status: "保留" });
  });

  test("posts portable backup and restore requests", async () => {
    const received: Request[] = [];
    const fetcher: typeof fetch = async (input, init) => {
      received.push(new Request(input, init));
      return new Response(JSON.stringify(received.length === 1
        ? { path: "D:/backup.krbackup", same_volume: false }
        : { target_root: "D:/restored", files_restored: 2, files_verified: 2, ok: true }), {
        headers: { "Content-Type": "application/json" },
        status: 200
      });
    };
    const client = new ApiClient("http://127.0.0.1:43127", "launch-token", fetcher);

    await client.createPortableBackup("D:/backup.krbackup", "passphrase");
    await client.restorePortableBackup("D:/backup.krbackup", "D:/restored", "passphrase");

    expect(received[0].url).toBe("http://127.0.0.1:43127/api/backup/portable");
    expect(await received[0].json()).toEqual({ target_path: "D:/backup.krbackup", passphrase: "passphrase" });
    expect(received[1].url).toBe("http://127.0.0.1:43127/api/backup/portable/restore");
    expect(await received[1].json()).toEqual({ backup_path: "D:/backup.krbackup", target_root: "D:/restored", passphrase: "passphrase" });
  });

  test("posts auditable corrections", async () => {
    const received: Request[] = [];
    const fetcher: typeof fetch = async (input, init) => {
      received.push(new Request(input, init));
      return new Response(JSON.stringify({ correction_id: "correction-1", entity_type: "candidate", field_name: "display_name", old_value: "张三", new_value: "张四", reverted: false }), {
        headers: { "Content-Type": "application/json" },
        status: 200
      });
    };
    const client = new ApiClient("http://127.0.0.1:43127", "launch-token", fetcher);

    await client.applyCorrection({
      entityType: "candidate",
      entityId: "candidate-1",
      fieldName: "display_name",
      newValue: "张四",
      reason: "姓名纠正"
    });

    expect(received[0].url).toBe("http://127.0.0.1:43127/api/correction/apply");
    expect(await received[0].json()).toEqual({ entity_type: "candidate", entity_id: "candidate-1", field_name: "display_name", new_value: "张四", reason: "姓名纠正" });
  });

  test("lists cases with backend pagination", async () => {
    let received: Request | undefined;
    const fetcher: typeof fetch = async (input, init) => {
      received = new Request(input, init);
      return new Response(JSON.stringify({ items: [], total: 0, page: 1, page_size: 20, has_more: false }), { status: 200 });
    };
    const client = new ApiClient("http://127.0.0.1:43127", "token", fetcher);

    await client.listCasesPage(2, 20, "jd-1");

    const url = new URL(received!.url);
    expect(url.pathname).toBe("/api/case");
    expect(url.searchParams.get("page")).toBe("2");
    expect(url.searchParams.get("page_size")).toBe("20");
    expect(url.searchParams.get("jd_id")).toBe("jd-1");
  });

  test("lists JDs with backend pagination and filters", async () => {
    let received: Request | undefined;
    const fetcher: typeof fetch = async (input, init) => {
      received = new Request(input, init);
      return new Response(JSON.stringify({ items: [], total: 0, page: 1, page_size: 10, has_more: false }), { status: 200 });
    };
    const client = new ApiClient("http://127.0.0.1:43127", "token", fetcher);

    await client.listJdsPage(1, 10, { title: "Java", company: "金融", status: "OPEN" });

    const url = new URL(received!.url);
    expect(url.pathname).toBe("/api/jd/page");
    expect(url.searchParams.get("page")).toBe("1");
    expect(url.searchParams.get("page_size")).toBe("10");
    expect(url.searchParams.get("title")).toBe("Java");
    expect(url.searchParams.get("company")).toBe("金融");
    expect(url.searchParams.get("status")).toBe("OPEN");
  });

  test("sends at most two ai connections and never puts keys in URLs", async () => {
    const requests: Request[] = [];
    const fetcher: typeof fetch = async (input, init) => {
      requests.push(new Request(input, init));
      return new Response(JSON.stringify({ protection_level: "dual", connections: [], catalog_version: 1 }), { status: 200 });
    };
    const client = new ApiClient("http://127.0.0.1:43127", "token", fetcher);
    await client.updateAiConfig({ connections: [
      { provider_id: "deepseek", display_name: "DeepSeek", api_key: "ds-secret", models: {}, enabled: true },
      { provider_id: "qwen", display_name: "通义千问", api_key: "qw-secret", models: {}, enabled: true },
    ] });
    expect(new URL(requests[0].url).pathname).toBe("/api/ai/config");
    expect(requests[0].url).not.toContain("secret");
    expect(await requests[0].json()).toHaveProperty("connections", expect.any(Array));
  });

  test("probe key is sent only in POST body", async () => {
    const requests: Request[] = [];
    const fetcher: typeof fetch = async (input, init) => {
      requests.push(new Request(input, init));
      return new Response(JSON.stringify({ auth: { ok: true, error_code: null, suggested_action: null } }), { status: 200 });
    };
    const client = new ApiClient("http://127.0.0.1:43127", "token", fetcher);
    await client.probeAiConnection({ provider_id: "deepseek", api_key: "probe-secret" });
    expect(requests[0].method).toBe("POST");
    expect(requests[0].url).not.toContain("probe-secret");
  });
});
