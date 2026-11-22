/* Execute inside Adobe Premiere, alongside the extracted Alpine Passage test bundle. */
(function () {
    var dir = new File($.fileName).parent;
    var src = new File(dir.fsName + "/Alpine-Passage.prproj");
    var out = new File(dir.fsName + "/Adobe-resaved.prproj");
    var report = new File(dir.fsName + "/Adobe-verification.txt");
    var markerName = "Splice Adobe round-trip";
    function line(text) { report.open("a"); report.writeln(text); report.close(); }
    function check(value, detail) { if (!value) throw Error(detail); }
    function same(path, file) { return new File(path).fsName === file.fsName; }
    function activeFile(file) {
        check(app.project && same(app.project.path, file), "Active project is not the requested verification copy");
        return app.project;
    }
    function readTimeline(project) {
        check(project.sequences.numSequences === 1, "Expected one sequence");
        var seq=project.sequences[0],settings=seq.getSettings(),v=seq.videoTracks[0],i;
        line("Sequence="+seq.name+" size="+settings.videoFrameWidth+"x"+settings.videoFrameHeight+
             " fps="+(254016000000/Number(seq.timebase))+" seconds="+(Number(seq.end)/254016000000));
        check(settings.videoFrameWidth===1920 && settings.videoFrameHeight===1080,"Unexpected sequence size");
        check(Math.abs(254016000000/Number(seq.timebase)-25)<0.001,"Expected 25 fps");
        check(v.clips.numItems===4,"Expected four shots on V1");
        for (i=0;i<v.clips.numItems;i++) {
            var clip=v.clips[i];
            line("V1["+i+"]="+clip.name+" "+clip.start.seconds+".."+clip.end.seconds);
            check(Math.abs(clip.start.seconds-i*8)<0.01 && Math.abs(clip.duration.seconds-8)<0.01,"Unexpected shot timing");
        }
        var transitions=v.transitions.numItems;
        line("Transitions="+transitions);
        check(transitions===3,"Expected three timeline transitions");
        for(i=0;i<transitions;i++)line("Transition="+v.transitions[i].name+" "+v.transitions[i].start.seconds+".."+v.transitions[i].end.seconds);
        var titles=0;
        for(i=1;i<seq.videoTracks.numTracks;i++) {
            var layer=seq.videoTracks[i];
            for(var j=0;j<layer.clips.numItems;j++){
                var title=layer.clips[j];
                if(Math.abs(title.start.seconds-.8)<.01 && Math.abs(title.duration.seconds-3)<.01)titles++;
                line("Graphic="+title.name+" "+title.start.seconds+".."+title.end.seconds);
            }
        }
        check(titles===1,"Expected one three-second opening title");
        var a=seq.audioTracks[0];check(a.clips.numItems===1,"Expected one audio score");
        line("Score="+a.clips[0].name+" seconds="+a.clips[0].duration.seconds);
        check(Math.abs(a.clips[0].duration.seconds-32)<.01,"Expected 32-second score");
        return seq;
    }
    function link(items, linked) {
        for(var i=0;i<items.numItems;i++){
            var item=items[i];
            if(item.children && item.children.numItems)link(item.children,linked);
            if(!item.canChangeMediaPath())continue;
            var old=item.getMediaPath(),base=String(old||item.name).split(/[\\/]/).pop();
            if(!/^(ridge\.mp4|valley\.mp4|forest\.mp4|lake\.mp4|score\.wav)$/.test(base))continue;
            var source=new File(dir.fsName+"/Media/"+base);
            check(source.exists,"Missing bundled media "+base);
            if(!same(old,source))item.changeMediaPath(source.fsName,true);
            check(same(item.getMediaPath(),source) && !item.isOffline(),"Failed relink "+base);
            linked[base]=true;
            line("Online="+base);
        }
    }
    try {
        check(src.exists,"Extract the test bundle beside this script");
        check(!out.exists && !report.exists,"Use a fresh test folder");
        line("Adobe Premiere "+app.version+" on "+$.os);
        line("Opening "+src.name);
        app.openDocument(src.fsName,1,1,1);
        var p=activeFile(src); var seq=readTimeline(p);
        p.saveAs(out.fsName);p=activeFile(out);
        var linked={};link(p.rootItem.children,linked);
        check(linked["ridge.mp4"] && linked["valley.mp4"] && linked["forest.mp4"] && linked["lake.mp4"] && linked["score.wav"],"All five sources must be online");
        seq=p.sequences[0];seq.markers.createMarker(12).name=markerName;
        p.save();
        check(out.exists && out.length>0,"No Adobe-resaved file");
        p.closeDocument(0,0);
        app.openDocument(out.fsName,1,1,1);
        p=activeFile(out);seq=readTimeline(p);
        var found=false;var marker=seq.markers.getFirstMarker();
        while(marker){if(marker.name===markerName && Math.abs(marker.start.seconds-12)<.01){found=true;break;}marker=seq.markers.getNextMarker(marker);}
        check(found,"Adobe edit absent after closing and reopening the saved copy");
        line("PASS: Adobe reopened its saved copy with the marker at 12.00 s.");
    }catch(error){line("FAIL: "+error+"; line "+error.line);}
})();
