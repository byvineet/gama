import type { TransitionName } from "../DisplayProtocol";

export function enterClass(name?: TransitionName): string {
  switch (name) {
    case "slide": return "gc-enter-slide";
    case "scale": return "gc-enter-scale";
    case "reveal": return "gc-enter-reveal";
    case "scan": return "gc-enter-scan";
    case "pulse": return "gc-enter-pulse";
    case "glow": return "gc-enter-glow";
    case "dissolve": return "gc-enter-fade";
    case "none": return "";
    case "fade":
    default:
      return "gc-enter-fade";
  }
}

export function exitClass(name?: TransitionName): string {
  switch (name) {
    case "slide": return "gc-exit-slide";
    case "scale": return "gc-exit-scale";
    case "dissolve": return "gc-exit-dissolve";
    case "none": return "";
    default:
      return "gc-exit-fade";
  }
}
