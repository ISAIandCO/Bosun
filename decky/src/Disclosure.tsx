// SPDX-License-Identifier: GPL-3.0-or-later
import { useState, ReactNode } from 'react';
import { ButtonItem, PanelSection, PanelSectionRow } from '@decky/ui';

export function Disclosure({ title, children, section = true }: { title: string; children: ReactNode; section?: boolean }) {
  const [open, setOpen] = useState(false);
  const content = <>
    <PanelSectionRow><ButtonItem layout="below" onClick={() => setOpen(v => !v)}>{`${open ? '▾' : '▸'} ${title}`}</ButtonItem></PanelSectionRow>
    {/* Keep mounted: collapsing must not discard a launch editor or pending native callback. */}
    <div style={{ display: open ? 'block' : 'none' }} aria-hidden={!open}>{children}</div>
  </>;
  return section ? <PanelSection>{content}</PanelSection> : content;
}
