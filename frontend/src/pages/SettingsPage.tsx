import { useMutation, useQuery } from "@tanstack/react-query";
import {
  getOllamaModels,
  getSettings,
  MODE_PRESETS,
  saveSettings,
  Settings,
} from "@/lib/api";
import { Card } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Loader2 } from "lucide-react";
import PageHeader from "@/components/PageHeader";

const IMAGE_PROVIDER_HINTS: Record<string, string> = {
  "workers-ai": "Cloudflare Workers AI — free tier, ~7s/image. Needs CF_ACCOUNT_ID + CF_API_TOKEN on the backend.",
  pollinations: "Keyless, no signup — lower quality. Good fallback.",
  gemini: "Gemini 2.5 Flash Image — good quality, but NOT free (needs billing on the Google project).",
  diffusers: "Local Stable Diffusion — requires a GPU and the local-gpu dependencies on the backend.",
};

export default function SettingsPage() {
  const { data: initial, isLoading } = useQuery({
    queryKey: ["settings"],
    queryFn: getSettings,
  });
  const [s, setS] = useState<Settings | null>(null);
  useEffect(() => {
    if (initial && !s) setS(initial);
  }, [initial, s]);

  const {
    data: modelsData,
    refetch: refetchModels,
    isFetching: testingConn,
  } = useQuery({
    queryKey: ["ollama-models", s?.ollamaUrl],
    queryFn: () => getOllamaModels(s?.ollamaUrl),
    enabled: s?.llmProvider === "ollama" && !!s?.ollamaUrl,
  });

  const testConnection = async () => {
    const r = await refetchModels();
    const models = r.data?.models ?? [];
    if (models.length) {
      toast.success(`Connected — ${models.length} model${models.length > 1 ? "s" : ""} found`);
      if (s && !models.includes(s.modelName)) setS({ ...s, modelName: models[0] });
    } else {
      toast.error("No models found. Is Ollama running and reachable from the server?");
    }
  };

  const mut = useMutation({
    mutationFn: (payload: Settings) => saveSettings(payload),
    onSuccess: (r) => {
      toast.success("Settings saved");
      setS(r);
    },
    onError: (e) => toast.error((e as { message?: string })?.message ?? "Save failed"),
  });

  if (isLoading || !s) {
    return (
      <div className="p-8">
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const update = <K extends keyof Settings>(k: K, v: Settings[K]) =>
    setS({ ...s, [k]: v });

  const onModeChange = (mode: Settings["imageMode"]) => {
    if (mode === "custom") {
      update("imageMode", "custom");
    } else {
      const preset = MODE_PRESETS[mode];
      setS({ ...s, imageMode: mode, ...preset });
    }
  };

  const customDisabled = s.imageMode !== "custom";
  const supportsModelAndGuidance = s.imageProvider === "diffusers";

  return (
    <div className="mx-auto max-w-3xl space-y-6 px-6 py-14 sm:py-16">
      <PageHeader
        kicker="Configuration"
        title="Settings"
        subtitle="Configure AI providers and image generation."
      />

      <Card className="p-6 space-y-4 shadow-card">
        <h2 className="font-semibold text-lg">Language Model</h2>
        <p className="text-xs text-muted-foreground">
          Drives page summaries, illustration prompts, and character extraction. Image
          generation is separate and always uses the hosted provider.
        </p>
        <div className="grid gap-2">
          <Label>Active provider</Label>
          <Select
            value={s.llmProvider}
            onValueChange={(v) => update("llmProvider", v)}
          >
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              {(["ollama", "groq", "gemini"] as const).map((p) => (
                <SelectItem key={p} value={p}>{p}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          {s.llmProvider !== "ollama" && (
            <p className="text-xs text-muted-foreground">
              Model{" "}
              <span className="font-mono">
                {s.llmProvider === "groq" ? s.groqModel : s.geminiModel}
              </span>{" "}
              — set by the <code>{s.llmProvider.toUpperCase()}_MODEL</code> env var on the
              backend.
            </p>
          )}
        </div>

        {s.llmProvider === "ollama" && (
          <>
            <div className="grid gap-2">
              <Label>Ollama URL</Label>
              <div className="flex gap-2">
                <Input
                  value={s.ollamaUrl}
                  placeholder="http://localhost:11434"
                  onChange={(e) => update("ollamaUrl", e.target.value)}
                />
                <Button
                  type="button"
                  variant="outline"
                  disabled={testingConn || !s.ollamaUrl}
                  onClick={testConnection}
                >
                  {testingConn ? <Loader2 className="size-4 animate-spin" /> : "Test"}
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">
                Must be reachable <strong>from the server</strong>. Local runs can use{" "}
                <code>localhost:11434</code>; against the hosted backend, expose your Ollama
                with a tunnel (ngrok / cloudflared) and paste that URL. This is a single
                shared setting for the whole site.
              </p>
            </div>
            <div className="grid gap-2">
              <Label>LLM Model</Label>
              <Select value={s.modelName} onValueChange={(v) => update("modelName", v)}>
                <SelectTrigger>
                  <SelectValue placeholder="Select model" />
                </SelectTrigger>
                <SelectContent>
                  {Array.from(
                    new Set([...(modelsData?.models ?? []), s.modelName].filter(Boolean)),
                  ).map((m) => (
                    <SelectItem key={m} value={m}>
                      {m}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {!modelsData?.models?.length && (
                <p className="text-xs text-muted-foreground">
                  Hit <strong>Test</strong> to load the models on that instance.
                </p>
              )}
            </div>
            <div className="grid gap-2">
              <Label>Timeout (sec)</Label>
              <Input
                type="number"
                value={s.timeout}
                onChange={(e) => update("timeout", Number(e.target.value))}
              />
            </div>
          </>
        )}
      </Card>

      <Card className="p-6 space-y-4 shadow-card">
        <div className="space-y-2">
          <h2 className="font-semibold text-lg">Image Generation</h2>
          <Label>Active provider</Label>
          <Select
            value={s.imageProvider}
            onValueChange={(v) => update("imageProvider", v)}
          >
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              {(["workers-ai", "pollinations", "gemini", "diffusers"] as const).map((p) => (
                <SelectItem key={p} value={p}>{p}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="text-xs text-muted-foreground">{IMAGE_PROVIDER_HINTS[s.imageProvider] ?? ""}</p>
          {!supportsModelAndGuidance && (
            <p className="text-xs text-muted-foreground">
              Model and guidance are fixed by this provider and not configurable here.
            </p>
          )}
        </div>
        <div className="grid sm:grid-cols-2 gap-4">
          <div className="grid gap-2">
            <Label>Image Mode</Label>
            <Select value={s.imageMode} onValueChange={(v) => onModeChange(v as Settings["imageMode"])}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                {(["quality", "balanced", "fast", "custom"] as const).map((m) => (
                  <SelectItem key={m} value={m}>{m}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="grid gap-2">
            <Label>Artistic Style</Label>
            <Select value={s.imageStyle} onValueChange={(v) => update("imageStyle", v as Settings["imageStyle"])}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                {(["normal", "storybook", "comic", "cinematic"] as const).map((m) => (
                  <SelectItem key={m} value={m}>{m}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        {supportsModelAndGuidance && (
          <div className="grid gap-2">
            <Label>Image Model</Label>
            <Input
              value={s.imageModel}
              disabled={customDisabled}
              onChange={(e) => update("imageModel", e.target.value)}
            />
          </div>
        )}

        <div className="grid sm:grid-cols-2 gap-4">
          <div className="grid gap-2">
            <Label>Width</Label>
            <Input
              type="number"
              value={s.imageWidth}
              disabled={customDisabled}
              onChange={(e) => update("imageWidth", Number(e.target.value))}
            />
          </div>
          <div className="grid gap-2">
            <Label>Height</Label>
            <Input
              type="number"
              value={s.imageHeight}
              disabled={customDisabled}
              onChange={(e) => update("imageHeight", Number(e.target.value))}
            />
          </div>
          <div className="grid gap-2">
            <Label>Steps</Label>
            {s.imageProvider === "workers-ai" && (
              <p className="text-xs text-muted-foreground">Capped at 20 by this provider.</p>
            )}
            <Input
              type="number"
              value={s.imageSteps}
              disabled={customDisabled}
              onChange={(e) => update("imageSteps", Number(e.target.value))}
            />
          </div>
          {supportsModelAndGuidance && (
            <div className="grid gap-2">
              <Label>Guidance</Label>
              <Input
                type="number"
                step="0.1"
                value={s.imageGuidance}
                disabled={customDisabled}
                onChange={(e) => update("imageGuidance", Number(e.target.value))}
              />
            </div>
          )}
        </div>
      </Card>

      <div className="flex justify-end">
        <Button
          className="bg-gradient-primary"
          disabled={mut.isPending}
          onClick={() => mut.mutate(s)}
        >
          {mut.isPending ? "Saving..." : "Save changes"}
        </Button>
      </div>
    </div>
  );
}
