/* Local WebGL viewer for ordinary XYZ/RGB point clouds. No external dependencies. */
class PointViewer {
  constructor(canvas, info) {
    this.canvas = canvas; this.info = info; this.yaw = 0; this.pitch = 0; this.zoom = .75; this.pan = [0, 0]; this.size = 2;
    this.gl = canvas.getContext('webgl', {antialias: true, preserveDrawingBuffer: true});
    if (!this.gl) throw Error('浏览器不支持 WebGL，无法显示点云。');
    const gl = this.gl;
    const shader = (type, source) => {const s = gl.createShader(type);gl.shaderSource(s, source);gl.compileShader(s);if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw Error(gl.getShaderInfoLog(s));return s;};
    this.program = gl.createProgram();
    gl.attachShader(this.program, shader(gl.VERTEX_SHADER, `attribute vec3 position;attribute vec3 color;uniform vec2 angles;uniform vec2 pan;uniform float zoom;uniform float aspect;uniform float size;varying vec3 rgb;void main(){float c=cos(angles.x),s=sin(angles.x);vec3 p=vec3(c*position.x+s*position.z,position.y,-s*position.x+c*position.z);c=cos(angles.y);s=sin(angles.y);p=vec3(p.x,c*p.y-s*p.z,s*p.y+c*p.z);gl_Position=vec4(p.x*zoom/aspect+pan.x,p.y*zoom+pan.y,p.z*.2,1.);gl_PointSize=size;rgb=color;}`));
    gl.attachShader(this.program, shader(gl.FRAGMENT_SHADER, `precision mediump float;varying vec3 rgb;void main(){vec2 p=gl_PointCoord-.5;if(dot(p,p)>.25)discard;gl_FragColor=vec4(rgb,1.);}`));
    gl.linkProgram(this.program);if(!gl.getProgramParameter(this.program,gl.LINK_STATUS)) throw Error(gl.getProgramInfoLog(this.program));
    this.buffer = gl.createBuffer(); this.count = 0;
    canvas.addEventListener('pointerdown', e => {canvas.setPointerCapture(e.pointerId);this.drag = {x:e.clientX,y:e.clientY};});
    canvas.addEventListener('pointerup', () => this.drag = null);
    canvas.addEventListener('pointercancel', () => this.drag = null);
    canvas.addEventListener('pointermove', e => {if(!this.drag)return;const dx=e.clientX-this.drag.x,dy=e.clientY-this.drag.y;if(e.shiftKey){this.pan[0]+=dx/canvas.clientWidth*2;this.pan[1]-=dy/canvas.clientHeight*2;}else{this.yaw+=dx*.008;this.pitch+=dy*.008;}this.drag={x:e.clientX,y:e.clientY};this.draw();});
    canvas.addEventListener('wheel', e => {e.preventDefault();this.zoom=Math.max(.05,Math.min(20,this.zoom*Math.exp(-e.deltaY*.001)));this.draw();},{passive:false});
    new ResizeObserver(()=>this.draw()).observe(canvas);
  }
  async load(run, kind) {
    const sequence = this.sequence = (this.sequence || 0) + 1;
    this.info.textContent = '正在读取点云…';
    const response = await fetch('/api/cloud?run='+encodeURIComponent(run)+'&kind='+encodeURIComponent(kind));
    if(!response.ok){const error=await response.json();throw Error(error.error);}
    const packet=await response.arrayBuffer();if(sequence!==this.sequence)return;
    const headerLength=new DataView(packet).getUint32(0,true);
    const metadata=JSON.parse(new TextDecoder().decode(new Uint8Array(packet,4,headerLength)));
    const points=new Float32Array(packet,4+headerLength);
    this.original=points;this.metadata=metadata;this.count=metadata.display_points;this.fit(false);
    this.info.textContent='显示 '+metadata.display_points.toLocaleString()+' / '+metadata.original_points.toLocaleString()+' 个点'+(metadata.sampled?'（抽样预览，原始结果保留全部点）':'');
  }
  fit(all=false){if(!this.original)return;const metadata=this.metadata,minimum=all?metadata.min:metadata.fit_min,maximum=all?metadata.max:metadata.fit_max;const center=minimum.map((v,i)=>(v+maximum[i])/2),scale=Math.max(...minimum.map((v,i)=>maximum[i]-v))/2||1;const points=new Float32Array(this.original);for(let i=0;i<points.length;i+=6)for(let axis=0;axis<3;axis++)points[i+axis]=(points[i+axis]-center[axis])/scale;this.gl.bindBuffer(this.gl.ARRAY_BUFFER,this.buffer);this.gl.bufferData(this.gl.ARRAY_BUFFER,points,this.gl.STATIC_DRAW);this.reset();}
  reset(){this.yaw=0;this.pitch=0;this.zoom=.75;this.pan=[0,0];this.draw();}
  draw(){const gl=this.gl,c=this.canvas,dpr=Math.min(devicePixelRatio||1,2);const w=Math.round(c.clientWidth*dpr),h=Math.round(c.clientHeight*dpr);if(!w||!h)return;c.width=w;c.height=h;gl.viewport(0,0,w,h);gl.clearColor(.035,.055,.095,1);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);if(!this.count)return;gl.enable(gl.DEPTH_TEST);gl.useProgram(this.program);gl.bindBuffer(gl.ARRAY_BUFFER,this.buffer);for(const [name,offset] of [['position',0],['color',12]]){const loc=gl.getAttribLocation(this.program,name);gl.enableVertexAttribArray(loc);gl.vertexAttribPointer(loc,3,gl.FLOAT,false,24,offset);}gl.uniform2f(gl.getUniformLocation(this.program,'angles'),this.yaw,this.pitch);gl.uniform2f(gl.getUniformLocation(this.program,'pan'),...this.pan);gl.uniform1f(gl.getUniformLocation(this.program,'zoom'),this.zoom);gl.uniform1f(gl.getUniformLocation(this.program,'aspect'),w/h);gl.uniform1f(gl.getUniformLocation(this.program,'size'),this.size*dpr);gl.drawArrays(gl.POINTS,0,this.count);}
}
window.PointViewer=PointViewer;
