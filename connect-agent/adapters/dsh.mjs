// The installer substitutes paths only; credentials stay in the local hook store.
import {spawn, spawnSync} from 'node:child_process';
import {readFileSync,writeFileSync,renameSync,mkdirSync} from 'node:fs';
import {join} from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
const PYTHON=__PM_PYTHON__, HOOK=__PM_HOOK__, CONFIG=__PM_CONFIG__;
export const name='personal-memory-automatic';
export const inject=['systemPrompt','workingDirectory'];
const textOf=m=>(m?.content??[]).filter(p=>p.type==='text').map(p=>p.text).join('\n');

export function installDshBridge(ctx, transport) {
  const active=new WeakMap(), sessions=new Map(), disposers=[];
  const section='personal-memory:current';
  let disposed=false;
  const clear=(session)=>{const entry=sessions.get(session.id);if(entry){active.delete(entry.agent);sessions.delete(session.id);}};
  disposers.push(ctx.on('agent/inbox/claimed',({agent,message,turn})=>{
    if(disposed||message?.source?.kind!=='user'||!message.id||!textOf(message).trim())return;
    const cwd=ctx.workingDirectory.get(agent.session);
    let entry=active.get(agent);
    if(!entry||entry.turn!==turn){
      entry={agent,turn,cwd,session:agent.session.id,firstSeq:agent.session.seq,
        turn_id:message.id,messages:new Map(),context:'',reading:null,revision:0};
      active.set(agent,entry);sessions.set(agent.session.id,entry);
    }
    if(!entry.messages.has(message.id)){
      entry.messages.set(message.id,{id:message.id,role:'user',text:textOf(message)});
      entry.prompt=[...entry.messages.values()].map(m=>m.text).join('\n').slice(-20000);
      entry.revision++;entry.context='';entry.reading=null;
    }
  }));
  disposers.push(ctx.systemPrompt.context({name:section,order:300,required:true,interpolate:false,
    text:context=>context.agent ? active.get(context.agent)?.context??'' : ''}));
  disposers.push(ctx.on('system-prompt/assemble',async(_assembly,context,next)=>{
    const assembly=await next(), entry=active.get(context.agent);
    if(disposed||!entry||!entry.prompt)return assembly;
    const revision=entry.revision;
    if(!entry.reading)entry.reading=Promise.resolve().then(()=>transport.read({
      hook_event_name:'context',session_id:entry.session,turn_id:entry.turn_id,
      prompt:entry.prompt,cwd:entry.cwd,workspace_root:entry.cwd
    })).catch(()=>({}));
    const result=await entry.reading;
    if(disposed||active.get(context.agent)!==entry||revision!==entry.revision)return assembly;
    entry.origin_project_id=result.scope?.project_id??null;
    entry.context=typeof result.context_text==='string'?result.context_text:'';
    const contribution=assembly.contexts.find(c=>c.name===section);
    if(contribution){contribution.text=entry.context;contribution.interpolate=false;}
    else if(entry.context)assembly.contexts.push({name:section,text:entry.context,interpolate:false});
    transport.receipt?.({session_id:entry.session,turn_id:entry.turn_id,trace_id:result.trace_id,
      scope:result.scope,read_outcome:result.read_outcome,
      matches:result.items?.map(m=>({memory_id:m.memory_id,version:m.version})),stage:entry.context?'context_ready':'no_context'});
    return assembly;
  }));
  const retry=entry=>{
    try{if(transport.capture(entry.completed)?.durable)clear(entry.agent.session);}catch{}
  };
  disposers.push(ctx.on('session/flush',session=>{const entry=sessions.get(session.id);if(entry?.completed)retry(entry);}));
  disposers.push(ctx.on('session/event',(session,event)=>{
    if(disposed||event.type!=='turn/end')return;
    const entry=sessions.get(session.id);
    if(!entry||entry.turn!==event.data.turn)return;
    if(event.data.reason?.kind!=='completed'){clear(session);return;}
    const values=[], calls=new Map(), results=new Map(), shadowed=new Set();
    // Match only results with the actual same-turn call event and its causal seq.
    for(let seq=entry.firstSeq;seq<event.seq;seq++){
      const e=session.eventAt(seq);if(!e)continue;
      if(e.surfaceOp==='replace')for(const ref of e.sourceEventSeqs??[])shadowed.add(ref);
      if(e.type==='tool/call'&&e.data.turn===entry.turn){
        const id=e.data.callId;
        if(calls.has(id))calls.set(id,null);else calls.set(id,e);
      }
      if(e.type==='tool/result'&&e.data.turn===entry.turn&&e.surfaceOp==='append'){
        const id=e.data.message?.toolCallId;
        if(!results.has(id))results.set(id,[]);results.get(id).push(e);
      }
    }
    // Consume only live, append-origin messages from this completed turn.
    // Runtime-context snapshots are user-role messages but are not human input.
    for(let seq=entry.firstSeq;seq<event.seq;seq++){
      const e=session.eventAt(seq);
      if(e?.surfaceOp!=='append')continue;
      if(e.type==='user/message'&&e.data.source?.kind==='user'&&entry.messages.has(e.data.id))
        values.push({id:e.data.id,role:'user',text:textOf(e.data)});
      else if(e.type==='assistant/message'&&e.data.turn===entry.turn){
        const message=e.data.message, text=textOf(message);
        if(message?.id&&text)values.push({id:message.id,role:'assistant',text});
      }
      else if(e.type==='tool/result'&&e.data.turn===entry.turn&&!shadowed.has(e.seq)){
        const message=e.data.message,id=message?.toolCallId,call=calls.get(id);
        const candidates=(results.get(id)??[]).filter(r=>!shadowed.has(r.seq));
        if(!call||candidates.length!==1||!e.sourceEventSeqs?.includes(call.seq)||e.data.step!==call.data.step)continue;
        const name=call.data.name;
        if(typeof name!=='string'||!/^[A-Za-z0-9_.:-]{1,160}$/.test(name)||typeof id!=='string'||!/^[A-Za-z0-9_-]{1,200}$/.test(id))continue;
        if(/personal[_-]memory/i.test(name))continue; // Never recollect injected/read-back PM data.
        const raw=textOf(message);if(!raw.trim())continue;
        const truncated=raw.length>12000;
        values.push({id:'tool:'+createHash('sha256').update(entry.session+':'+entry.turn+':'+id).digest('hex'),role:'tool',
          text:'工具 '+name+' 执行观察：\n'+raw.slice(0,12000),
          tool_observation:{name,call_id:id,state:message.isError===true||e.data.error?'error':'result',truncated}});
      }
    }
    if(!values.some(m=>m.role==='assistant')||!values.some(m=>m.role==='user'))return;
    entry.completed={hook_event_name:'capture',session_id:entry.session,
      turn_id:entry.turn_id,cwd:entry.cwd,workspace_root:entry.cwd,
      origin_project_id:entry.origin_project_id,capture_version:2,completion_state:'completed',messages:values};
    retry(entry);
  }));
  const dispose=()=>{disposed=true;for(const fn of disposers.reverse())fn?.();sessions.clear();};
  return dispose;
}

export function apply(ctx){
  const status=state=>{
    try{
      const folder=JSON.parse(readFileSync(CONFIG,'utf8')).state_dir;mkdirSync(folder,{recursive:true});
      const destination=join(folder,'adapter-status.json'),temporary=destination+'.'+randomUUID()+'.tmp';
      writeFileSync(temporary,JSON.stringify({state,pid:process.pid,at:Date.now(),adapter:'dsh-cordis-v2-tools'}));renameSync(temporary,destination);
    }catch{}
  };
  const receipt=value=>{
    try{
      const state=JSON.parse(readFileSync(CONFIG,'utf8')).state_dir;
      mkdirSync(state,{recursive:true});
      const hash=createHash('sha256').update(value.session_id).digest('hex');
      const row={...value,at:Date.now(),note:'Context delivery is not proof of final answer use'};
      for(const name of ['read-receipt.json','read-'+hash+'.json']){
        const destination=join(state,name),temporary=destination+'.'+randomUUID()+'.tmp';
        writeFileSync(temporary,JSON.stringify(row));renameSync(temporary,destination);
      }
    }catch{/* Diagnostics never interrupt the host. */}
  };
  const capture=event=>{
    try{
      const child=spawnSync(PYTHON,['-X','utf8',HOOK,'--config',CONFIG],{input:JSON.stringify(event),encoding:'utf8',windowsHide:true,timeout:1200,maxBuffer:128000});
      return JSON.parse(child.stdout||'{}');
    }catch{return {};}
  };
  const read=event=>new Promise(resolve=>{
    const child=spawn(PYTHON,['-X','utf8',HOOK,'--config',CONFIG],{windowsHide:true,stdio:['pipe','pipe','ignore']});
    let output='',settled=false;
    const finish=value=>{if(!settled){settled=true;clearTimeout(timer);resolve(value);}};
    const timer=setTimeout(()=>{child.kill();finish({read_outcome:{outcome:'process_timeout'}});},1600);
    child.stdout.setEncoding('utf8');
    child.stdout.on('data',data=>{
      if(settled)return;
      output+=data;
      if(output.length>128000){child.kill();finish({});}
      else if(output.includes('\n')){try{finish(JSON.parse(output));}catch{finish({});}}
    });
    child.on('error',()=>finish({}));child.on('close',()=>{try{finish(JSON.parse(output));}catch{finish({});}});
    child.stdin.on('error',()=>{});child.stdin.end(JSON.stringify(event));
  });
  const dispose=installDshBridge(ctx,{read,capture,receipt});
  status('active');
  ctx.effect(()=>()=>{dispose();status('inactive');});
}
