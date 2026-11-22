import type {TripPresentation} from "./api";
import {Markdown} from "./Markdown";
import {TransportLegCard} from "./TransportLegCard";

export function TripMessage({presentation,onFill,onQuote}:{presentation:TripPresentation;onFill?:(text:string)=>void;onQuote?:()=>void}) {
  const {trip,sections,prefix,suffix}=presentation;
  return <div className="trip-message">
    {presentation.lifecycle && ["saved","discarded","superseded"].includes(presentation.lifecycle.state) && <p className="muted" role="status">
      历史预览 · {({saved:"已保存",discarded:"已放弃",superseded:"已被新修订替代"} as Record<string,string>)[presentation.lifecycle.state]}。当前版本可在行程面板查看。
    </p>}
    {prefix && <Markdown text={prefix}/>}
    {sections.map((section,index)=>section.kind==="text" ? <Markdown key={index} text={section.text}/> :
      trip.plan.intercity && <TransportLegCard key={section.direction} journey={trip.plan.intercity}
        leg={trip.plan.intercity[section.direction]} direction={section.direction} tripId={trip.trip_id} onFill={onFill}
        onAdjust={onQuote && onFill ? ()=>{onQuote();onFill(`${section.direction==="outbound"?"去程":"返程"}改为：`);} : undefined}/>)}
    {suffix && <Markdown text={suffix}/>}
  </div>;
}
