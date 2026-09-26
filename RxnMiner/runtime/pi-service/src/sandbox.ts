import type { MountManifest } from "./contracts.js";
import { RuntimePolicyError } from "./errors.js";

export interface SandboxLease {
  id: string;
  userId: string;
  jobId: string;
  endpoint: string;
  expiresAt: string;
}

export interface SandboxLaunchSecrets {
  provider: string;
  credential: string;
  expiresAt?: string;
}

export interface SandboxManager {
  create(manifest: MountManifest, secrets?: SandboxLaunchSecrets): Promise<SandboxLease>;
  destroy(lease: SandboxLease): Promise<void>;
}

export class UnconfiguredSandboxManager implements SandboxManager {
  async create(_manifest: MountManifest, _secrets?: SandboxLaunchSecrets): Promise<SandboxLease> {
    throw new RuntimePolicyError(
      "REMOTE_SANDBOX_NOT_CONFIGURED",
      "A remote OS-level sandbox provider is required; a local cwd is not an isolation boundary",
      503,
    );
  }
  async destroy(): Promise<void> {}
}

export class RemoteSandboxManager implements SandboxManager {
  constructor(
    private readonly baseUrl: string,
    private readonly serviceToken: string,
  ) {
    if (!baseUrl.startsWith("https://")) {
      throw new Error("Remote sandbox endpoint must use HTTPS");
    }
  }

  async create(manifest: MountManifest, secrets?: SandboxLaunchSecrets): Promise<SandboxLease> {
    const response = await fetch(`${this.baseUrl}/v1/sandboxes`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${this.serviceToken}`,
        "content-type": "application/json",
      },
      body: JSON.stringify({
        tenant: manifest.userId,
        job: manifest.jobId,
        session: manifest.sessionId,
        networkPolicy: "deny-by-default",
        resourceProfile: "chemrxn-agent",
        mounts: manifest.mounts,
        ...(secrets ? {
          runtimeSecrets: {
            provider: secrets.provider,
            credential: secrets.credential,
            expiresAt: secrets.expiresAt,
          },
        } : {}),
      }),
    });
    if (!response.ok) throw new Error(`Sandbox provider returned HTTP ${response.status}`);
    const lease = (await response.json()) as SandboxLease;
    if (lease.userId !== manifest.userId || lease.jobId !== manifest.jobId) {
      throw new Error("Sandbox provider returned a lease for the wrong tenant or job");
    }
    return lease;
  }

  async destroy(lease: SandboxLease): Promise<void> {
    await fetch(`${this.baseUrl}/v1/sandboxes/${encodeURIComponent(lease.id)}`, {
      method: "DELETE",
      headers: { authorization: `Bearer ${this.serviceToken}` },
    });
  }
}
