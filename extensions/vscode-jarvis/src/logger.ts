/**
 * Minimal OutputChannel wrapper. All extension log lines flow through
 * a single channel so operators can grep/save from one place.
 *
 * Never uses console.log — Output channel is the only sink.
 */

import * as vscode from 'vscode';

export class Logger {
  private readonly channel: vscode.OutputChannel;

  public constructor(name: string) {
    this.channel = vscode.window.createOutputChannel(name);
  }

  public info(msg: string): void {
    this.channel.appendLine(`[${timestamp()}] INFO  ${msg}`);
  }

  public warn(msg: string): void {
    this.channel.appendLine(`[${timestamp()}] WARN  ${msg}`);
  }

  public error(msg: string, exc?: unknown): void {
    const extra =
      exc === undefined
        ? ''
        : ` :: ${exc instanceof Error ? exc.message : String(exc)}`;
    this.channel.appendLine(`[${timestamp()}] ERROR ${msg}${extra}`);
  }

  public show(): void {
    this.channel.show(true);
  }

  public dispose(): void {
    this.channel.dispose();
  }
}

function timestamp(): string {
  const d = new Date();
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}` +
    `T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}Z`
  );
}

function pad(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}
