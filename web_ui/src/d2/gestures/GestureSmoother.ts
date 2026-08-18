/** Temporal smoothing + hysteresis for landmarks / gestures. */

export class OneEuroFilter {
  private minCutoff: number;
  private beta: number;
  private dCutoff: number;
  private xPrev: number | null = null;
  private dxPrev = 0;
  private tPrev: number | null = null;

  constructor(minCutoff = 1.0, beta = 0.007, dCutoff = 1.0) {
    this.minCutoff = minCutoff;
    this.beta = beta;
    this.dCutoff = dCutoff;
  }

  private alpha(cutoff: number, dt: number) {
    const tau = 1 / (2 * Math.PI * cutoff);
    return 1 / (1 + tau / dt);
  }

  filter(x: number, t: number): number {
    if (this.tPrev == null || this.xPrev == null) {
      this.tPrev = t;
      this.xPrev = x;
      return x;
    }
    const dt = Math.max(1e-6, (t - this.tPrev) / 1000);
    const dx = (x - this.xPrev) / dt;
    const edx = this.dxPrev + this.alpha(this.dCutoff, dt) * (dx - this.dxPrev);
    const cutoff = this.minCutoff + this.beta * Math.abs(edx);
    const out = this.xPrev + this.alpha(cutoff, dt) * (x - this.xPrev);
    this.xPrev = out;
    this.dxPrev = edx;
    this.tPrev = t;
    return out;
  }

  reset() {
    this.xPrev = null;
    this.dxPrev = 0;
    this.tPrev = null;
  }
}

export class HysteresisGate {
  private state = false;
  constructor(
    private enterThreshold: number,
    private exitThreshold: number,
  ) {}

  update(value: number): boolean {
    if (this.state) {
      if (value < this.exitThreshold) this.state = false;
    } else {
      if (value > this.enterThreshold) this.state = true;
    }
    return this.state;
  }

  get() { return this.state; }
  reset() { this.state = false; }
}
