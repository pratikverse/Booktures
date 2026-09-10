import { apiClient } from "./apiClient";
import type { Settings } from "./types";

type BackendSettings = {
  llm_provider: string;
  llm_model: string;
  groq_model: string;
  gemini_model: string;
  image_provider: string;
  ollama_url: string;
  model_name: string;
  timeout: number;
  image_mode: Settings["imageMode"];
  image_model: string;
  image_width: number;
  image_height: number;
  image_steps: number;
  image_guidance: number;
  imageStyle: Settings["imageStyle"];
};

function toFrontendSettings(b: BackendSettings): Settings {
  return {
    llmProvider: b.llm_provider,
    llmModel: b.llm_model,
    groqModel: b.groq_model,
    geminiModel: b.gemini_model,
    imageProvider: b.image_provider,
    ollamaUrl: b.ollama_url,
    modelName: b.model_name,
    timeout: b.timeout,
    imageMode: b.image_mode,
    imageModel: b.image_model,
    imageWidth: b.image_width,
    imageHeight: b.image_height,
    imageSteps: b.image_steps,
    imageGuidance: b.image_guidance,
    imageStyle: b.imageStyle,
  };
}

function toBackendSettings(s: Settings) {
  return {
    llm_provider: s.llmProvider,
    image_provider: s.imageProvider,
    ollama_url: s.ollamaUrl,
    model_name: s.modelName,
    timeout: s.timeout,
    image_mode: s.imageMode,
    image_model: s.imageModel,
    image_width: s.imageWidth,
    image_height: s.imageHeight,
    image_steps: s.imageSteps,
    image_guidance: s.imageGuidance,
    imageStyle: s.imageStyle,
  };
}

export async function getSettings(): Promise<Settings> {
  const { data } = await apiClient.get<BackendSettings>("/settings");
  return toFrontendSettings(data);
}

export async function getOllamaModels(
  url?: string,
): Promise<{ models: string[]; reachable?: boolean; url?: string }> {
  const { data } = await apiClient.get<{ models: string[]; reachable?: boolean; url?: string }>(
    "/settings/ollama-models",
    { params: url ? { url } : undefined },
  );
  return data;
}

export async function saveSettings(settings: Settings): Promise<Settings> {
  const { data } = await apiClient.put<BackendSettings>("/settings", toBackendSettings(settings));
  return toFrontendSettings(data);
}
