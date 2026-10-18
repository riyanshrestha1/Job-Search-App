// Run from the repository root: node tests/test_resume_feedback.cjs
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../h1bfinder/templates/index.html'),'utf8');
const start=html.indexOf('// Resume feedback is only shown'),end=html.indexOf('async function startScan()',start);
assert.ok(start>=0&&end>start);
const source=html.slice(start,end);
function setup(){
 const nodes=new Map(),timers=new Map();let next=0;
 const $=s=>{if(!nodes.has(s))nodes.set(s,{textContent:'',hidden:true,dataset:{},style:{},disabled:false,files:[],value:'',setAttribute(k,v){this[k]=v;}});return nodes.get(s);};
 const c={console,Number,Array,String,$,FormData:class{append(){}},setTimeout(fn,ms){const id=++next;timers.set(id,{fn,ms});return id;},clearTimeout(id){timers.delete(id);}};
 vm.createContext(c);vm.runInContext('let resumeBusy=false,resumeRevision=0,savedResumeProfile=null,resumeFeedback="initial";',c);vm.runInContext(source,c);
 return {c,$,timers,run:s=>vm.runInContext(s,c)};
}
(async()=>{
 let passed=0;async function test(name,fn){await fn();passed++;console.log('ok - '+name);}
 const profile={chars:350,skills:['python','sql'],years:6};
 await test('page load and repeated status polling never show an upload message',()=>{const t=setup();t.c.renderResumeProfile(null);t.c.renderResumeProfile(profile);t.c.renderResumeProfile(profile);assert.equal(t.$('#resume-status').hidden,true);assert.equal(t.$('#k_resume').textContent,'Ready for matching');assert.equal(t.timers.size,0);});
 await test('new successful upload shows once and disappears after six seconds',async()=>{const t=setup();t.c.api=async()=>profile;t.$('#rfile').files=[{name:'resume.pdf'}];await t.c.uploadResume();assert.equal(t.$('#resume-status').hidden,false);assert.equal(t.$('#resume-status').dataset.state,'success');const timer=[...t.timers.values()][0];assert.equal(timer.ms,6000);timer.fn();t.c.renderResumeProfile(profile);assert.equal(t.$('#resume-status').hidden,true);});
 await test('pending upload prevents duplicates',async()=>{const t=setup();let resolve,calls=0;t.c.api=()=>{calls++;return new Promise(r=>resolve=r);};t.$('#rfile').files=[{name:'resume.pdf'}];const p=t.c.uploadResume();await t.c.uploadResume();assert.equal(calls,1);assert.equal(t.$('#resume-upload-button').disabled,true);resolve(profile);await p;assert.equal(t.$('#resume-upload-button').disabled,false);});
 await test('failed upload preserves active resume and keeps readable error',async()=>{const t=setup();t.c.renderResumeProfile(profile);t.c.api=async()=>{throw Error('Unreadable file');};t.$('#rfile').files=[{name:'broken.pdf'}];await t.c.uploadResume();assert.equal(t.$('#resume-status').dataset.state,'error');assert.equal(t.run('savedResumeProfile'),profile);assert.equal(t.timers.size,0);});
 await test('selection clears a prior success timer',()=>{const t=setup();t.c.setResumeFeedback('success','Saved','Saved');t.$('#rfile').files=[{name:'next.pdf'}];t.c.onResumeSelected();assert.equal(t.timers.size,0);assert.equal(t.$('#resume-status').dataset.state,'selected');});
 await test('missing file and short text do not reach server',async()=>{const t=setup();let calls=0;t.c.api=()=>calls++;await t.c.uploadResume();t.$('#rtext').value='short';await t.c.uploadResume(true);assert.equal(calls,0);assert.equal(t.$('#resume-status').dataset.state,'error');});
 await test('malformed response cannot confirm a save',async()=>{const t=setup();t.c.api=async()=>({ok:true});t.$('#rfile').files=[{name:'resume.txt'}];await t.c.uploadResume();assert.equal(t.$('#resume-status').dataset.state,'error');});
 await test('pasted resume confirms success and clears text',async()=>{const t=setup();t.c.api=async()=>profile;t.$('#rtext').value='Engineer experience '.repeat(10);await t.c.uploadResume(true);assert.equal(t.$('#resume-status').dataset.state,'success');assert.equal(t.$('#rtext').value,'');});
 console.log(passed+' checks passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
