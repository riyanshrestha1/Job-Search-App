// Run from the repository root: node tests/test_resume_feedback.cjs
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../h1bfinder/templates/index.html'),'utf8');
const start=html.indexOf('// Resume feedback is based on server-confirmed saves');
const end=html.indexOf('async function startScan()',start);
assert.ok(start>=0&&end>start,'Resume functions exist in the actual template');
const source=html.slice(start,end);

function setup(){
 const nodes=new Map();
 function element(){return {textContent:'',dataset:{},style:{},disabled:false,files:[],value:'',children:[],
   replaceChildren(...children){this.children=children;},append(child){this.children.push(child);},
   setAttribute(key,value){this[key]=value;},scrollIntoView(){}};}
 const context={console,Number,Array,String,FormData:class{append(){}},document:{createElement:element},
   $:selector=>{if(!nodes.has(selector))nodes.set(selector,element());return nodes.get(selector);}};
 vm.createContext(context);
 vm.runInContext('let resumeBusy=false,resumeRevision=0,savedResumeProfile=null,resumeFeedback="initial";',context);
 vm.runInContext(source,context);
 return {context,nodes,$:context.$,run:code=>vm.runInContext(code,context)};
}

(async()=>{
 let passed=0;
 async function test(name,fn){await fn();passed++;console.log('ok - '+name);}
 const profile={chars:350,skills:['python','sql'],years:6};

 await test('empty status explicitly says no resume uploaded',()=>{
  const t=setup();t.context.renderResumeProfile(null);
  assert.equal(t.$('#resume-badge').textContent,'No resume uploaded');
  assert.equal(t.$('#k_resume').textContent,'Not uploaded yet');
 });
 await test('saved resume is restored from server status after reload',()=>{
  const t=setup();t.context.renderResumeProfile(profile);
  assert.equal(t.$('#resume-badge').dataset.saved,'true');
  assert.equal(t.$('#resume-status-title').textContent,'Your resume is saved');
  assert.equal(t.$('#rprof').style.display,'block');
 });
 await test('file selection is not presented as a successful upload',()=>{
  const t=setup();t.$('#rfile').files=[{name:"Riyan's resume.pdf"}];t.context.onResumeSelected();
  assert.equal(t.$('#resume-status').dataset.state,'selected');
  assert.match(t.$('#resume-status-message').textContent,/Click Upload resume/);
  assert.equal(t.run('savedResumeProfile'),null);
 });
 await test('missing files and short pasted text do not make requests',async()=>{
  const t=setup();let calls=0;t.context.api=()=>{calls++;};
  await t.context.uploadResume();
  assert.equal(t.$('#resume-status-title').textContent,'No file selected');
  t.$('#rtext').value='short';await t.context.uploadResume(true);
  assert.equal(calls,0);assert.equal(t.$('#resume-status').dataset.state,'error');
 });
 await test('pending uploads disable controls and prevent duplicate submissions',async()=>{
  const t=setup();let complete,calls=0;t.context.api=()=>{calls++;return new Promise(resolve=>complete=resolve);};
  t.$('#rfile').files=[{name:'resume.pdf'}];
  const pending=t.context.uploadResume();
  assert.equal(t.$('#resume-status').dataset.state,'uploading');
  assert.equal(t.$('#resume-upload-button').disabled,true);
  assert.equal(t.$('#resume-paste-button').disabled,true);
  await t.context.uploadResume();assert.equal(calls,1);
  complete(profile);await pending;
  assert.equal(t.$('#resume-upload-button').disabled,false);
  assert.equal(t.$('#resume-status').dataset.state,'success');
  assert.match(t.$('#resume-status-message').textContent,/resume.pdf was saved/);
  assert.equal(t.$('#resume-badge').textContent,'✓ Resume uploaded');
 });
 await test('pasted text receives its own success confirmation',async()=>{
  const t=setup();t.context.api=async()=>profile;t.$('#rtext').value='Data engineer. '.repeat(10);
  await t.context.uploadResume(true);
  assert.equal(t.$('#resume-status-title').textContent,'✓ Resume text saved successfully');
 });
 await test('failed replacements keep the saved profile and show an inline error',async()=>{
  const t=setup();t.context.renderResumeProfile(profile);
  t.context.api=async()=>{throw new Error('Could not read this PDF');};
  t.$('#rfile').files=[{name:'unreadable.pdf'}];await t.context.uploadResume();
  assert.equal(t.$('#resume-status').dataset.state,'error');
  assert.match(t.$('#resume-status-message').textContent,/Saved resume details are shown below/);
  assert.equal(t.$('#resume-badge').dataset.saved,'true');
  assert.equal(t.$('#rfile').disabled,false);
 });
 await test('malformed responses never show a successful save',async()=>{
  const t=setup();t.context.api=async()=>({ok:true});t.$('#rfile').files=[{name:'resume.pdf'}];
  await t.context.uploadResume();assert.equal(t.$('#resume-status').dataset.state,'error');
  assert.equal(t.run('savedResumeProfile'),null);
 });
 await test('a readable resume with no detected skills still counts as saved',async()=>{
  const t=setup();t.context.api=async()=>({chars:100,skills:[],years:0});t.$('#rfile').files=[{name:'resume.txt'}];
  await t.context.uploadResume();assert.equal(t.$('#resume-status').dataset.state,'success');
 });
 console.log(`${passed} resume feedback checks passed`);
})().catch(error=>{console.error(error);process.exitCode=1;});
