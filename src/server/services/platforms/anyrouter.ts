import { config } from '../../config.js';
import type { BalanceInfo, CheckinResult, TokenVerifyResult, UserInfo, CheckinCredentials } from './base.js';
import { NewApiAdapter } from './newApi.js';

/**
 * anyrouter / agentrouter 前面挂着阿里云 ESA，下发的是 aliyun_waf_aa /
 * aliyun_waf_bb 那套新版 JS 挑战 —— 必须执行页面脚本才能通过。
 *
 * NewApiAdapter 全靠纯 HTTP（含它自己 newApiShield.ts 里那套 node:vm 求解，
 * 只认老的 `var arg1` + acw_sc__v2 形式），所以 login / verifyToken / checkin /
 * getBalance 对这两个站都不可靠，实测症状：
 *   - 添加账号报 "Token verification timed out (10s)"
 *   - 签到间歇返回 "Unexpected token '<'"（拿到的是挑战页 HTML）
 *   - 面板上余额一直不动（getBalance 静默失败）
 *
 * 因此这几个方法转发给本机的浏览器能力后端（CloakBrowser 真浏览器），由它把
 * 挑战解掉再请求接口。后端是**无状态 RPC**：调度、连接管理、签到日志、消息推送
 * 仍全部由 metapi 负责，避免两套系统各推一份通知、各记一份日志。
 *
 * 注意：这是破坏性 fork，不与上游保持兼容。
 */
const EXT_BASE =
  process.env.EXT_WAF_URL || 'http://host.docker.internal:4100/__checkin/ext';

// 起浏览器进程、过 JS 挑战、跳转 /console 再请求接口，单次 20-60 秒是常态。
// 调用方那几个原本 10 秒的硬编码超时也已一并放大，否则永远等不到结果。
const EXT_TIMEOUT_MS = Number(process.env.EXT_WAF_TIMEOUT_MS || 240_000);

async function extCall<T>(action: string, payload: Record<string, unknown>): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), EXT_TIMEOUT_MS);
  try {
    const res = await fetch(`${EXT_BASE}/${action}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        // 后端复用 metapi 的 AUTH_TOKEN 鉴权（它只监听回环与 docker 网桥，不对公网开放）
        Authorization: `Bearer ${config.authToken}`,
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!res.ok) {
      throw new Error(`浏览器能力后端 HTTP ${res.status}（${action}）`);
    }
    const data = (await res.json()) as unknown;
    if (data && typeof data === 'object' && 'error' in (data as Record<string, unknown>)) {
      throw new Error(String((data as Record<string, unknown>).error));
    }
    return data as T;
  } catch (err) {
    const e = err as { name?: string; message?: string };
    if (e?.name === 'AbortError') {
      throw new Error(
        `浏览器能力后端超时（${Math.round(EXT_TIMEOUT_MS / 1000)}s），检查 checkin-panel 服务`,
      );
    }
    throw new Error(e?.message || '浏览器能力后端不可用');
  } finally {
    clearTimeout(timer);
  }
}

export class AnyRouterAdapter extends NewApiAdapter {
  readonly platformName = 'anyrouter';

  async detect(url: string): Promise<boolean> {
    const normalized = (url || '').toLowerCase();
    return normalized.includes('anyrouter') || normalized.includes('agentrouter');
  }

  override async login(
    baseUrl: string,
    username: string,
    password: string,
  ): Promise<{
    success: boolean;
    accessToken?: string;
    username?: string;
    message?: string;
    platformUserId?: number;
  }> {
    try {
      return await extCall<{
        success: boolean;
        accessToken?: string;
        username?: string;
        message?: string;
        platformUserId?: number;
      }>('login', { baseUrl, username, password });
    } catch (err) {
      return { success: false, message: (err as Error).message };
    }
  }

  override async verifyToken(
    baseUrl: string,
    token: string,
    platformUserId?: number,
  ): Promise<TokenVerifyResult> {
    try {
      return await extCall<TokenVerifyResult>('verify', { baseUrl, token, platformUserId });
    } catch (err) {
      // 返回 unknown 让上层走它自己的失败提示，但把原因带出来便于排查
      console.warn(`[anyrouter] verifyToken 失败: ${(err as Error).message}`);
      return { tokenType: 'unknown' };
    }
  }

  override async getUserInfo(
    baseUrl: string,
    accessToken: string,
    platformUserId?: number,
  ): Promise<UserInfo | null> {
    try {
      const info = await extCall<UserInfo>('userinfo', {
        baseUrl,
        token: accessToken,
        platformUserId,
      });
      return info && info.username ? info : null;
    } catch (err) {
      console.warn(`[anyrouter] getUserInfo 失败: ${(err as Error).message}`);
      return null;
    }
  }

  override async getBalance(
    baseUrl: string,
    accessToken: string,
    platformUserId?: number,
  ): Promise<BalanceInfo> {
    // 这里不吞异常：余额读不到应当让调用方知道，否则会把 0 当成真实余额写库
    return await extCall<BalanceInfo>('balance', {
      baseUrl,
      token: accessToken,
      platformUserId,
    });
  }

  override async checkin(
    baseUrl: string,
    accessToken: string,
    platformUserId?: number,
    credentials?: CheckinCredentials,
  ): Promise<CheckinResult> {
    try {
      // agentrouter has no check-in endpoint at all — its daily quota is granted
      // when you log in (verified: quota went from 45455841 to 57955841, +$25,
      // after one full browser login). So the backend needs the credentials to
      // do the check-in at all; sites with a real endpoint ignore them.
      return await extCall<CheckinResult>('checkin', {
        baseUrl,
        token: accessToken,
        platformUserId,
        username: credentials?.username,
        password: credentials?.password,
      });
    } catch (err) {
      return { success: false, message: (err as Error).message };
    }
  }
}
